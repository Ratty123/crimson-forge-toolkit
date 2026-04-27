from __future__ import annotations

import dataclasses
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from cdmw.core.mod_package import (
    MeshLooseModFile,
    ModPackageExportOptions,
    normalize_mod_package_payload_path,
    resolve_mod_package_root,
    write_mesh_loose_mod_package_metadata,
)
from cdmw.models import ArchiveEntry, ArchiveModelTextureReference, ModPackageInfo


MATERIAL_SIDECAR_EXTENSIONS = frozenset({".pami", ".pac_xml", ".pam_xml", ".pamlod_xml"})
MATERIAL_SIDECAR_XML_BASENAME_SUFFIXES = (".pac.xml", ".pam.xml", ".pamlod.xml")
MATERIAL_SIDECAR_PREVIEW_MODEL_EXTENSIONS = frozenset({".pac", ".pam", ".pamlod"})
MATERIAL_SIDECAR_COLOR_TAGS = frozenset({"RepresentColor"})
MATERIAL_SIDECAR_COLOR_PARAMETER_TAGS = frozenset({"MaterialParameterColor"})
MATERIAL_SIDECAR_FLOAT_PARAMETER_TAGS = frozenset({"MaterialParameterFloat"})
MATERIAL_SIDECAR_TEXTURE_PARAMETER_TAGS = frozenset({"MaterialParameterTexture"})
MATERIAL_SIDECAR_EDITABLE_KINDS = frozenset({"color", "float", "texture"})
_COLOR_ATTRS = ("x", "y", "z", "r", "g", "b", "_x", "_y", "_z", "_r", "_g", "_b")
_RGB_ATTR_GROUPS = (("x", "y", "z"), ("r", "g", "b"), ("_x", "_y", "_z"), ("_r", "_g", "_b"))
_VALUE_ATTRS = ("Value", "_value", "value")
_TEXTURE_ATTRS = ("Value", "_value", "value", "_path", "path", "Path", "File", "file", "Texture", "texture")


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarEditableValue:
    row_id: str
    kind: str
    group_label: str
    parameter_name: str
    value: str
    detail: str = ""


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarEditResult:
    text: str
    changed_rows: tuple[str, ...]


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarRelatedFile:
    entry: ArchiveEntry
    confidence: str
    reason: str
    include_by_default: bool = True


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarExportResult:
    package_root: Path
    written_files: tuple[Path, ...]
    metadata_files: tuple[Path, ...]


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarPreviewModelCandidate:
    entry: ArchiveEntry
    confidence: str
    reason: str


@dataclasses.dataclass(slots=True, frozen=True)
class MaterialSidecarPreviewOverride:
    group_label: str
    tint_color: tuple[float, float, float] = ()
    brightness: float = 1.0
    uv_scale: float = 1.0
    confidence: str = "low"
    reason: str = ""


def is_material_sidecar_entry(entry: ArchiveEntry) -> bool:
    extension = str(getattr(entry, "extension", "") or "").strip().lower()
    basename = PurePosixPath(str(getattr(entry, "path", "") or "").replace("\\", "/")).name.lower()
    return extension in MATERIAL_SIDECAR_EXTENSIONS or (
        extension == ".xml" and basename.endswith(MATERIAL_SIDECAR_XML_BASENAME_SUFFIXES)
    )


def _strip_namespace(tag: str) -> str:
    text = str(tag or "")
    if "}" in text:
        return text.rsplit("}", 1)[-1]
    return text


def _first_attr(element: ET.Element, names: Sequence[str]) -> str:
    for name in names:
        value = str(element.attrib.get(name) or "").strip()
        if value:
            return value
    return ""


def _parameter_name(element: ET.Element) -> str:
    return _first_attr(
        element,
        ("_name", "StringItemID", "ParameterName", "parameterName", "_parameterName", "Name", "name", "ID", "id"),
    )


def _normalize_fragment(text: str) -> str:
    value = str(text or "").replace("\ufeff", "").replace("\x00", "").strip()
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", value, count=1, flags=re.IGNORECASE)


def _parse_wrapped_fragment(text: str) -> ET.Element:
    normalized = _normalize_fragment(text)
    return ET.fromstring(f"<Root>{normalized}</Root>")


def _element_indexes(root: ET.Element) -> dict[int, int]:
    return {id(element): index for index, element in enumerate(root.iter())}


def _row_id(kind: str, element_index: int, parameter_name: str, target_attr: str = "") -> str:
    normalized_parameter = re.sub(r"[^a-z0-9_]+", "_", str(parameter_name or "").strip().lower()).strip("_")
    normalized_attr = re.sub(r"[^a-z0-9_]+", "_", str(target_attr or "").strip().lower()).strip("_")
    return ":".join(part for part in (kind, str(element_index), normalized_parameter, normalized_attr) if part)


def _parse_float(value: str) -> float | None:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return None


def _format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _parse_hex_color(value: str) -> tuple[float, float, float, str] | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", text):
        return None
    normalized = text.lstrip("#")
    alpha = normalized[6:8] if len(normalized) >= 8 else ""
    return (
        int(normalized[0:2], 16) / 255.0,
        int(normalized[2:4], 16) / 255.0,
        int(normalized[4:6], 16) / 255.0,
        alpha,
    )


def _format_hex_color(values: Sequence[float], alpha: str = "") -> str:
    channels = []
    for value in list(values)[:3]:
        channels.append(f"{max(0, min(255, round(float(value) * 255))):02x}")
    while len(channels) < 3:
        channels.append("00")
    normalized_alpha = str(alpha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{2}", normalized_alpha):
        channels.append(normalized_alpha)
    return "#" + "".join(channels)


def _color_from_value_attr(raw_value: str) -> tuple[float, float, float]:
    parsed_hex = _parse_hex_color(raw_value)
    if parsed_hex is not None:
        return parsed_hex[:3]
    numbers: list[float] = []
    for token in re.split(r"[\s,;]+", str(raw_value or "").strip()):
        parsed = _parse_float(token)
        if parsed is None:
            continue
        numbers.append(parsed)
        if len(numbers) >= 3:
            break
    return tuple(numbers[:3]) if len(numbers) >= 3 else ()


def _color_target(element: ET.Element) -> tuple[tuple[float, float, float], tuple[str, ...], str]:
    for attrs in _RGB_ATTR_GROUPS:
        values: list[float] = []
        for attr in attrs:
            parsed = _parse_float(str(element.attrib.get(attr) or ""))
            if parsed is None:
                values = []
                break
            values.append(parsed)
        if len(values) == 3:
            return (values[0], values[1], values[2]), attrs, "attrs"
    values: list[float] = []
    attrs_found: list[str] = []
    for attr in _COLOR_ATTRS:
        parsed = _parse_float(str(element.attrib.get(attr) or ""))
        if parsed is None:
            continue
        values.append(parsed)
        attrs_found.append(attr)
        if len(values) >= 3:
            return (values[0], values[1], values[2]), tuple(attrs_found[:3]), "attrs"
    for attr in _VALUE_ATTRS:
        value = _color_from_value_attr(str(element.attrib.get(attr) or ""))
        if value:
            return value, (attr,), "value"
    return (), (), ""


def _format_color(values: Sequence[float]) -> str:
    if len(values) < 3:
        return ""
    return ", ".join(_format_float(float(value)) for value in values[:3])


def _clamp_color_channel(value: object, *, upper: float = 2.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(float(upper), parsed))


def _weighted_average_color(colors: Sequence[tuple[tuple[float, float, float], float]]) -> tuple[float, float, float]:
    total_weight = sum(max(0.0, float(weight)) for _color, weight in colors)
    if total_weight <= 0.0:
        return ()
    return (
        sum(_clamp_color_channel(color[0]) * max(0.0, float(weight)) for color, weight in colors) / total_weight,
        sum(_clamp_color_channel(color[1]) * max(0.0, float(weight)) for color, weight in colors) / total_weight,
        sum(_clamp_color_channel(color[2]) * max(0.0, float(weight)) for color, weight in colors) / total_weight,
    )


def _preview_color_weight(parameter_name: str) -> tuple[float, str]:
    normalized = re.sub(r"[^a-z0-9]+", "", str(parameter_name or "").strip().lower())
    if normalized == "representcolor":
        return 0.75, "represent color fallback"
    if normalized in {"_tintcolor", "tintcolor"}:
        return 6.0, "explicit tint color"
    if normalized in {"_baseheighttintcolor", "baseheighttintcolor"}:
        return 5.0, "base height tint color"
    if normalized in {"_tintcolorr", "_tintcolorg", "_tintcolorb", "tintcolorr", "tintcolorg", "tintcolorb"}:
        return 4.0, "dye tint channel approximation"
    if normalized in {
        "_dyeingcolormaskr",
        "_dyeingcolormaskg",
        "_dyeingcolormaskb",
        "dyeingcolormaskr",
        "dyeingcolormaskg",
        "dyeingcolormaskb",
    }:
        return 2.5, "dye mask approximation"
    if normalized in {
        "_dyeingdetaillayercolormaskr",
        "_dyeingdetaillayercolormaskg",
        "_dyeingdetaillayercolormaskb",
        "dyeingdetaillayercolormaskr",
        "dyeingdetaillayercolormaskg",
        "dyeingdetaillayercolormaskb",
    }:
        return 1.5, "detail dye mask approximation"
    if "tintcolor" in normalized or ("dyeing" in normalized and "color" in normalized):
        return 1.0, "material dye color approximation"
    return 0.0, ""


def _parse_color_edit(value: str) -> tuple[float, float, float] | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed_hex = _parse_hex_color(text)
    if parsed_hex is not None:
        return parsed_hex[:3]
    values = _color_from_value_attr(text)
    return values if values else None


def _set_color_value(element: ET.Element, attrs: Sequence[str], mode: str, value: str) -> bool:
    parsed = _parse_color_edit(value)
    if parsed is None:
        return False
    if mode == "value" and attrs:
        original_value = str(element.attrib.get(attrs[0]) or "").strip()
        original_hex = _parse_hex_color(original_value)
        edited_hex = _parse_hex_color(value)
        if original_hex is not None:
            alpha = edited_hex[3] if edited_hex is not None and edited_hex[3] else original_hex[3]
            element.set(attrs[0], _format_hex_color(parsed, alpha))
        else:
            element.set(attrs[0], _format_color(parsed))
        return True
    if len(attrs) >= 3:
        for attr, channel in zip(attrs[:3], parsed):
            element.set(attr, _format_float(float(channel)))
        return True
    return False


def _texture_value_target(parameter: ET.Element) -> tuple[ET.Element | None, str]:
    for attr in _TEXTURE_ATTRS:
        value = str(parameter.attrib.get(attr) or "").strip()
        if _looks_like_texture_reference(value):
            return parameter, attr
    for child in parameter.iter():
        if child is parameter:
            continue
        tag_name = _strip_namespace(child.tag)
        normalized_tag = re.sub(r"[^a-z0-9]+", "", tag_name.lower())
        if not (
            tag_name == "ResourceReferencePath_ITexture"
            or "textureref" in normalized_tag
            or ("resourcereferencepath" in normalized_tag and "texture" in normalized_tag)
            or ("texture" in normalized_tag and any(token in normalized_tag for token in ("resource", "reference", "path", "file")))
        ):
            continue
        for attr in _TEXTURE_ATTRS:
            value = str(child.attrib.get(attr) or "").strip()
            if _looks_like_texture_reference(value):
                return child, attr
    return None, ""


def _looks_like_texture_reference(value: str) -> bool:
    return bool(re.search(r"\.(dds|png|jpg|jpeg|tga|bmp|tif|tiff)\b", str(value or ""), re.IGNORECASE))


def _display_color_value(element: ET.Element, values: Sequence[float], attrs: Sequence[str], mode: str) -> str:
    if mode == "value" and attrs:
        raw_value = str(element.attrib.get(attrs[0]) or "").strip()
        if _parse_hex_color(raw_value) is not None:
            return raw_value
    return _format_color(values)


def _group_label_for(element: ET.Element, stack: Sequence[ET.Element]) -> str:
    chain = [*stack, element]
    for candidate in reversed(chain):
        tag_name = _strip_namespace(candidate.tag)
        if tag_name == "SkinnedMeshMaterialWrapper":
            value = _first_attr(candidate, ("_subMeshName", "subMeshName", "SubMeshName", "PrimitiveName", "primitiveName"))
            if value:
                return value
        if tag_name == "Material":
            value = _first_attr(candidate, ("PrimitiveName", "primitiveName", "SubMeshName", "subMeshName"))
            if value:
                return value
    for candidate in reversed(chain):
        tag_name = _strip_namespace(candidate.tag)
        if tag_name == "Material":
            value = _first_attr(candidate, ("_materialName", "MaterialName", "materialName"))
            if value:
                return value
        if tag_name == "SkinnedMeshMaterialWrapper":
            for child in candidate:
                if _strip_namespace(child.tag) == "Material":
                    value = _first_attr(child, ("_materialName", "MaterialName", "materialName"))
                    if value:
                        return value
    for candidate in reversed(chain):
        value = _first_attr(candidate, ("Name", "name"))
        if value:
            return value
    return "Global"


def _shader_label_for(element: ET.Element, stack: Sequence[ET.Element]) -> str:
    for candidate in reversed([*stack, element]):
        if _strip_namespace(candidate.tag) == "Material":
            value = _first_attr(candidate, ("_materialName", "MaterialName", "materialName"))
            if value:
                return value
        if _strip_namespace(candidate.tag) == "SkinnedMeshMaterialWrapper":
            for child in candidate:
                if _strip_namespace(child.tag) == "Material":
                    value = _first_attr(child, ("_materialName", "MaterialName", "materialName"))
                    if value:
                        return value
    return ""


def _material_detail(base_detail: str, element: ET.Element, stack: Sequence[ET.Element]) -> str:
    shader_label = _shader_label_for(element, stack)
    if shader_label:
        return f"{base_detail} | Shader: {shader_label}"
    return base_detail


def discover_material_sidecar_values(sidecar_text: str) -> tuple[MaterialSidecarEditableValue, ...]:
    try:
        root = _parse_wrapped_fragment(sidecar_text)
    except ET.ParseError:
        return ()
    indexes = _element_indexes(root)
    rows: list[MaterialSidecarEditableValue] = []

    def walk(element: ET.Element, stack: tuple[ET.Element, ...] = ()) -> None:
        tag_name = _strip_namespace(element.tag)
        group_label = _group_label_for(element, stack)
        element_index = indexes.get(id(element), 0)
        if tag_name in MATERIAL_SIDECAR_COLOR_TAGS:
            values, attrs, mode = _color_target(element)
            if values and attrs:
                rows.append(
                    MaterialSidecarEditableValue(
                        row_id=_row_id("color", element_index, tag_name, ",".join(attrs)),
                        kind="color",
                        group_label=group_label,
                        parameter_name=tag_name,
                        value=_display_color_value(element, values, attrs, mode),
                        detail=_material_detail("Material display color", element, stack),
                    )
                )
        elif tag_name in MATERIAL_SIDECAR_COLOR_PARAMETER_TAGS:
            parameter = _parameter_name(element) or tag_name
            values, attrs, mode = _color_target(element)
            if values and attrs:
                rows.append(
                    MaterialSidecarEditableValue(
                        row_id=_row_id("color", element_index, parameter, ",".join(attrs)),
                        kind="color",
                        group_label=group_label,
                        parameter_name=parameter,
                        value=_display_color_value(element, values, attrs, mode),
                        detail=_material_detail("Material color parameter", element, stack),
                    )
                )
        elif tag_name in MATERIAL_SIDECAR_FLOAT_PARAMETER_TAGS:
            parameter = _parameter_name(element) or tag_name
            target_attr = next((attr for attr in _VALUE_ATTRS if _parse_float(str(element.attrib.get(attr) or "")) is not None), "")
            if target_attr:
                rows.append(
                    MaterialSidecarEditableValue(
                        row_id=_row_id("float", element_index, parameter, target_attr),
                        kind="float",
                        group_label=group_label,
                        parameter_name=parameter,
                        value=str(element.attrib.get(target_attr) or "").strip(),
                        detail=_material_detail("Material scalar parameter", element, stack),
                    )
                )
        elif tag_name in MATERIAL_SIDECAR_TEXTURE_PARAMETER_TAGS:
            parameter = _parameter_name(element) or tag_name
            target, target_attr = _texture_value_target(element)
            if target is not None and target_attr:
                rows.append(
                    MaterialSidecarEditableValue(
                        row_id=_row_id("texture", element_index, parameter, target_attr),
                        kind="texture",
                        group_label=group_label,
                        parameter_name=parameter,
                        value=str(target.attrib.get(target_attr) or "").strip(),
                        detail=_material_detail("Material texture path", element, stack),
                    )
                )
        for child in element:
            walk(child, (*stack, element))

    walk(root)
    kind_order = {"color": 0, "float": 1, "texture": 2}
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                kind_order.get(row.kind, 99),
                row.group_label.lower(),
                row.parameter_name.lower(),
                row.value.lower(),
            ),
        )
    )


def discover_material_sidecar_preview_overrides(sidecar_text: str) -> tuple[MaterialSidecarPreviewOverride, ...]:
    try:
        root = _parse_wrapped_fragment(sidecar_text)
    except ET.ParseError:
        return ()
    overrides: list[MaterialSidecarPreviewOverride] = []

    for material in root.iter():
        tag_name = _strip_namespace(material.tag)
        if tag_name not in {"Root", "SkinnedMeshMaterialWrapper", "Material"}:
            continue
        if tag_name == "Root":
            children = list(material)
            if any(_strip_namespace(child.tag) in {"SkinnedMeshMaterialWrapper", "Material"} for child in children):
                continue
        group_label = _group_label_for(material, ())
        weighted_colors: list[tuple[tuple[float, float, float], float]] = []
        reasons: list[str] = []
        brightness = 1.0
        uv_scale = 1.0
        has_brightness = False
        has_uv_scale = False

        for child in material.iter():
            child_tag = _strip_namespace(child.tag)
            if child_tag == "RepresentColor":
                values, _attrs, _mode = _color_target(child)
                if values:
                    weight, reason = _preview_color_weight("RepresentColor")
                    if weight > 0:
                        weighted_colors.append((values, weight))
                        reasons.append(reason)
                continue
            if child_tag == "MaterialParameterColor":
                parameter = _parameter_name(child)
                values, _attrs, _mode = _color_target(child)
                if values:
                    weight, reason = _preview_color_weight(parameter)
                    if weight > 0:
                        weighted_colors.append((values, weight))
                        reasons.append(reason)
                continue
            if child_tag != "MaterialParameterFloat":
                continue
            parameter = re.sub(r"[^a-z0-9]+", "", _parameter_name(child).strip().lower())
            target_attr = next((attr for attr in _VALUE_ATTRS if _parse_float(str(child.attrib.get(attr) or "")) is not None), "")
            if not target_attr:
                continue
            parsed = _parse_float(str(child.attrib.get(target_attr) or ""))
            if parsed is None:
                continue
            if parameter in {"_brightness", "brightness"}:
                brightness = max(0.1, min(3.0, float(parsed)))
                has_brightness = True
            elif parameter in {"_uvscale", "uvscale"}:
                uv_scale = max(0.05, min(64.0, float(parsed)))
                has_uv_scale = True

        tint_color = _weighted_average_color(weighted_colors)
        if not tint_color and not has_brightness and not has_uv_scale:
            continue
        unique_reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
        confidence = "explicit" if any("explicit" in reason for reason in unique_reasons) else ("approximate" if tint_color else "scalar")
        overrides.append(
            MaterialSidecarPreviewOverride(
                group_label=group_label,
                tint_color=tint_color,
                brightness=brightness,
                uv_scale=uv_scale,
                confidence=confidence,
                reason=", ".join(unique_reasons) if unique_reasons else "scalar material preview parameter",
            )
        )
    seen_keys: set[tuple[str, tuple[float, ...], float, float]] = set()
    unique_overrides: list[MaterialSidecarPreviewOverride] = []
    for override in overrides:
        key = (
            override.group_label.lower(),
            tuple(round(float(value), 6) for value in override.tint_color),
            round(float(override.brightness), 6),
            round(float(override.uv_scale), 6),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_overrides.append(override)
    return tuple(unique_overrides)


def discover_material_sidecar_preview_overrides_for_edits(
    sidecar_text: str,
    edited_values: Mapping[str, str],
) -> tuple[MaterialSidecarPreviewOverride, ...]:
    if not edited_values:
        return ()
    try:
        root = _parse_wrapped_fragment(sidecar_text)
    except ET.ParseError:
        return ()
    indexes = _element_indexes(root)
    grouped: dict[str, dict[str, object]] = {}

    def group_state(group_label: str) -> dict[str, object]:
        return grouped.setdefault(
            group_label,
            {
                "colors": [],
                "reasons": [],
                "brightness": 1.0,
                "uv_scale": 1.0,
                "has_brightness": False,
                "has_uv_scale": False,
            },
        )

    def walk(element: ET.Element, stack: tuple[ET.Element, ...] = ()) -> None:
        tag_name = _strip_namespace(element.tag)
        element_index = indexes.get(id(element), 0)
        group_label = _group_label_for(element, stack)
        if tag_name in MATERIAL_SIDECAR_COLOR_TAGS or tag_name in MATERIAL_SIDECAR_COLOR_PARAMETER_TAGS:
            parameter = tag_name if tag_name in MATERIAL_SIDECAR_COLOR_TAGS else (_parameter_name(element) or tag_name)
            _values, attrs, _mode = _color_target(element)
            if not attrs:
                for child in element:
                    walk(child, (*stack, element))
                return
            row_id = _row_id("color", element_index, parameter, ",".join(attrs))
            if row_id in edited_values:
                parsed_color = _parse_color_edit(str(edited_values.get(row_id) or ""))
                if parsed_color is not None:
                    weight, reason = _preview_color_weight(parameter)
                    if weight <= 0:
                        weight = 1.0
                        reason = "edited color parameter"
                    state = group_state(group_label)
                    colors = state["colors"]
                    reasons = state["reasons"]
                    if isinstance(colors, list):
                        colors.append((parsed_color, weight))
                    if isinstance(reasons, list):
                        reasons.append(reason)
        elif tag_name == "MaterialParameterFloat":
            parameter_name = _parameter_name(element) or tag_name
            target_attr = next((attr for attr in _VALUE_ATTRS if _parse_float(str(element.attrib.get(attr) or "")) is not None), "")
            if target_attr:
                row_id = _row_id("float", element_index, parameter_name, target_attr)
                if row_id in edited_values:
                    parsed_float = _parse_float(str(edited_values.get(row_id) or ""))
                    if parsed_float is not None:
                        parameter = re.sub(r"[^a-z0-9]+", "", parameter_name.strip().lower())
                        state = group_state(group_label)
                        reasons = state["reasons"]
                        if parameter in {"_brightness", "brightness"}:
                            state["brightness"] = max(0.1, min(3.0, float(parsed_float)))
                            state["has_brightness"] = True
                            if isinstance(reasons, list):
                                reasons.append("edited brightness parameter")
                        elif parameter in {"_uvscale", "uvscale"}:
                            state["uv_scale"] = max(0.05, min(64.0, float(parsed_float)))
                            state["has_uv_scale"] = True
                            if isinstance(reasons, list):
                                reasons.append("edited UV scale parameter")
        for child in element:
            walk(child, (*stack, element))

    walk(root)

    overrides: list[MaterialSidecarPreviewOverride] = []
    for group_label, state in grouped.items():
        colors = state.get("colors")
        reasons = state.get("reasons")
        tint_color = _weighted_average_color(colors if isinstance(colors, list) else [])
        has_brightness = bool(state.get("has_brightness"))
        has_uv_scale = bool(state.get("has_uv_scale"))
        if not tint_color and not has_brightness and not has_uv_scale:
            continue
        unique_reasons = tuple(dict.fromkeys(reason for reason in (reasons if isinstance(reasons, list) else []) if reason))
        overrides.append(
            MaterialSidecarPreviewOverride(
                group_label=group_label,
                tint_color=tint_color,
                brightness=float(state.get("brightness") or 1.0),
                uv_scale=float(state.get("uv_scale") or 1.0),
                confidence="edited",
                reason=", ".join(unique_reasons) if unique_reasons else "edited material preview parameter",
            )
        )
    return tuple(overrides)


def apply_material_sidecar_edits(
    sidecar_text: str,
    edited_values: Mapping[str, str],
) -> MaterialSidecarEditResult:
    if not edited_values:
        return MaterialSidecarEditResult(text=_normalize_fragment(sidecar_text), changed_rows=())
    try:
        root = _parse_wrapped_fragment(sidecar_text)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse material sidecar XML: {exc}") from exc
    original_rows = {row.row_id: row for row in discover_material_sidecar_values(sidecar_text)}
    indexes = _element_indexes(root)
    changed_rows: list[str] = []

    for element in root.iter():
        element_index = indexes.get(id(element), 0)
        tag_name = _strip_namespace(element.tag)
        candidate_rows = [
            row
            for row in original_rows.values()
            if row.row_id in edited_values and row.row_id.startswith(("color:", "float:", "texture:"))
        ]
        if not candidate_rows:
            continue
        parameter = _parameter_name(element) or tag_name
        for original in candidate_rows:
            parts = original.row_id.split(":")
            if len(parts) < 2 or parts[1] != str(element_index):
                continue
            new_value = str(edited_values.get(original.row_id) or "").strip()
            if new_value == original.value:
                continue
            if original.kind == "color":
                _values, attrs, mode = _color_target(element)
                if not _set_color_value(element, attrs, mode, new_value):
                    raise ValueError(f"Invalid color value for {parameter}: {new_value}")
            elif original.kind == "float":
                parsed = _parse_float(new_value)
                if parsed is None:
                    raise ValueError(f"Invalid float value for {parameter}: {new_value}")
                target_attr = next((attr for attr in _VALUE_ATTRS if attr in element.attrib), "")
                if not target_attr:
                    raise ValueError(f"Could not locate float value attribute for {parameter}.")
                element.set(target_attr, _format_float(parsed))
            elif original.kind == "texture":
                target, target_attr = _texture_value_target(element)
                if target is None or not target_attr:
                    raise ValueError(f"Could not locate texture path for {parameter}.")
                target.set(target_attr, new_value.replace("\\", "/"))
            changed_rows.append(original.row_id)

    parts = [ET.tostring(child, encoding="unicode", short_empty_elements=True) for child in list(root)]
    text = "\n".join(part.strip() for part in parts if part.strip())
    return MaterialSidecarEditResult(text=text, changed_rows=tuple(changed_rows))


def _normalized_path(value: str) -> str:
    return PurePosixPath(str(value or "").replace("\\", "/").strip()).as_posix().lower()


def _family_stem(path: str) -> str:
    stem = PurePosixPath(path.replace("\\", "/")).stem.lower()
    for suffix in ("_op", "_n", "_normal", "_m", "_ma", "_mg", "_sp", "_mask", "_ao", "_d", "_height", "_emi", "_emc"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _is_preview_model_entry(entry: ArchiveEntry | None) -> bool:
    return isinstance(entry, ArchiveEntry) and str(getattr(entry, "extension", "") or "").strip().lower() in MATERIAL_SIDECAR_PREVIEW_MODEL_EXTENSIONS


def _sidecar_model_candidate_basenames(sidecar_path: str) -> tuple[str, ...]:
    basename = PurePosixPath(str(sidecar_path or "").replace("\\", "/")).name.strip().lower()
    if not basename:
        return ()
    stems: list[str] = []
    for suffix in (".pac_xml", ".pam_xml", ".pamlod_xml", ".pami", ".xml"):
        if basename.endswith(suffix):
            stems.append(basename[: -len(suffix)])
    if basename.endswith(".pac.xml") or basename.endswith(".pam.xml") or basename.endswith(".pamlod.xml"):
        stems.append(basename[:-4])
    if not stems:
        stems.append(PurePosixPath(basename).stem.lower())
    candidate_basenames: list[str] = []

    def add(value: str) -> None:
        normalized = str(value or "").strip().lower()
        if normalized and normalized not in candidate_basenames:
            candidate_basenames.append(normalized)

    for stem in stems:
        nested_extension = PurePosixPath(stem).suffix.lower()
        if nested_extension in MATERIAL_SIDECAR_PREVIEW_MODEL_EXTENSIONS:
            add(stem)
            nested_stem = PurePosixPath(stem).stem.lower()
            if nested_stem.endswith("_op"):
                add(f"{nested_stem[:-3]}{nested_extension}")
            continue
        for extension in (".pac", ".pam", ".pamlod"):
            add(f"{stem}{extension}")
            if stem.endswith("_op"):
                add(f"{stem[:-3]}{extension}")
    return tuple(candidate_basenames)


def _extract_material_sidecar_linked_model_paths(sidecar_text: str) -> tuple[str, ...]:
    try:
        root = _parse_wrapped_fragment(sidecar_text)
    except ET.ParseError:
        return ()
    paths: list[str] = []

    def add(raw_value: object) -> None:
        value = str(raw_value or "").replace("\\", "/").strip()
        if not value:
            return
        lowered = value.lower()
        if not any(lowered.endswith(extension) for extension in MATERIAL_SIDECAR_PREVIEW_MODEL_EXTENSIONS):
            return
        if value not in paths:
            paths.append(value)

    for element in root.iter():
        for attr in ("Path", "path", "_path", "File", "file", "_file", "Value", "_value", "value"):
            add(element.attrib.get(attr))
    return tuple(paths)


def detect_material_sidecar_preview_model_candidates(
    sidecar_entry: ArchiveEntry,
    *,
    sidecar_text: str = "",
    current_entry: ArchiveEntry | None = None,
    references: Sequence[ArchiveModelTextureReference] = (),
    archive_entries_by_basename: Mapping[str, Sequence[ArchiveEntry]] | None = None,
    archive_entries_by_normalized_path: Mapping[str, Sequence[ArchiveEntry]] | None = None,
) -> tuple[MaterialSidecarPreviewModelCandidate, ...]:
    candidates: dict[str, MaterialSidecarPreviewModelCandidate] = {}
    score_by_path: dict[str, int] = {}
    order_by_path: dict[str, int] = {}
    confidence_score = {"current": 4, "explicit": 3, "same-stem": 2, "family": 1}

    def add(entry: ArchiveEntry | None, confidence: str, reason: str) -> None:
        if not _is_preview_model_entry(entry):
            return
        key = _normalized_path(entry.path)
        if not key:
            return
        score = confidence_score.get(confidence, 0)
        existing_score = score_by_path.get(key, -1)
        if score > existing_score:
            candidates[key] = MaterialSidecarPreviewModelCandidate(entry, confidence, reason)
            score_by_path[key] = score
            order_by_path.setdefault(key, len(order_by_path))

    if _is_preview_model_entry(current_entry):
        add(current_entry, "current", "Current Archive Browser model selection")

    for reference in references:
        resolved_entry = getattr(reference, "resolved_entry", None)
        if _is_preview_model_entry(resolved_entry):
            add(resolved_entry, "explicit", "Explicit referenced model")
        linked_path = str(getattr(reference, "linked_mesh_path", "") or "").replace("\\", "/").strip().lower()
        if linked_path and archive_entries_by_normalized_path:
            for entry in archive_entries_by_normalized_path.get(linked_path, ()):
                add(entry, "explicit", "Explicit linked mesh path from sidecar reference")

    if sidecar_text and archive_entries_by_normalized_path:
        for linked_path in _extract_material_sidecar_linked_model_paths(sidecar_text):
            normalized = linked_path.replace("\\", "/").strip().lower()
            for entry in archive_entries_by_normalized_path.get(normalized, ()):
                add(entry, "explicit", "Explicit mesh path in material sidecar")

    if archive_entries_by_basename:
        for basename in _sidecar_model_candidate_basenames(sidecar_entry.path):
            for entry in archive_entries_by_basename.get(basename, ()):
                add(entry, "same-stem", "Same-stem material sidecar model")

        source_family = _family_stem(sidecar_entry.path)
        for basename in _sidecar_model_candidate_basenames(source_family):
            for entry in archive_entries_by_basename.get(basename, ()):
                add(entry, "family", "Known suffix-family material sidecar model")

    return tuple(
        sorted(
            candidates.values(),
            key=lambda item: (
                confidence_score.get(item.confidence, 0),
                -order_by_path.get(_normalized_path(item.entry.path), 0),
                item.entry.path.lower(),
            ),
            reverse=True,
        )
    )


def detect_material_sidecar_related_files(
    sidecar_entry: ArchiveEntry,
    *,
    references: Sequence[ArchiveModelTextureReference] = (),
    archive_entries_by_basename: Mapping[str, Sequence[ArchiveEntry]] | None = None,
) -> tuple[MaterialSidecarRelatedFile, ...]:
    related: dict[str, MaterialSidecarRelatedFile] = {}
    source_path = str(getattr(sidecar_entry, "path", "") or "").replace("\\", "/")
    source_basename = PurePosixPath(source_path).name.lower()
    source_stem = PurePosixPath(source_path).stem.lower()
    source_family = _family_stem(source_path)

    def add(entry: ArchiveEntry, confidence: str, reason: str, include_by_default: bool = True) -> None:
        if entry.path == sidecar_entry.path:
            return
        key = _normalized_path(entry.path)
        if not key:
            return
        existing = related.get(key)
        order = {"explicit": 3, "same-stem": 2, "family": 1, "unresolved": 0}
        if existing is None or order.get(confidence, 0) > order.get(existing.confidence, 0):
            related[key] = MaterialSidecarRelatedFile(entry, confidence, reason, include_by_default)

    for reference in references:
        resolved_entry = getattr(reference, "resolved_entry", None)
        if isinstance(resolved_entry, ArchiveEntry):
            add(resolved_entry, "explicit", "Explicit sidecar or preview reference")

    if archive_entries_by_basename:
        candidate_basenames = {
            source_basename,
            f"{source_stem}.pac",
            f"{source_stem}.pam",
            f"{source_stem}.pamlod",
            f"{source_stem}.dds",
            f"{source_stem}_op.dds",
            f"{source_stem}_n.dds",
            f"{source_stem}_normal.dds",
            f"{source_stem}_m.dds",
            f"{source_stem}_ma.dds",
            f"{source_stem}_mg.dds",
        }
        if source_stem.endswith(".pac"):
            nested_stem = PurePosixPath(source_stem).stem.lower()
            candidate_basenames.update({f"{nested_stem}.pac", f"{nested_stem}.dds", f"{nested_stem}_op.dds"})
        for basename in candidate_basenames:
            for entry in archive_entries_by_basename.get(basename.lower(), ()):
                add(entry, "same-stem", "Same-stem material sidecar or texture companion")
        for entries in archive_entries_by_basename.values():
            for entry in entries:
                entry_path = str(getattr(entry, "path", "") or "").replace("\\", "/")
                entry_family = _family_stem(entry_path)
                if entry_family and source_family and entry_family == source_family:
                    add(entry, "family", "Texture family suffix match")

    return tuple(sorted(related.values(), key=lambda item: (item.confidence, item.entry.path.lower()), reverse=True))


def export_material_sidecar_mod_package(
    *,
    edited_entry: ArchiveEntry,
    edited_text: str,
    related_entries: Sequence[ArchiveEntry],
    parent_root: Path,
    package_info: ModPackageInfo,
    export_options: ModPackageExportOptions | None = None,
    create_no_encrypt_file: bool = True,
    read_entry_bytes: Callable[[ArchiveEntry], bytes],
    on_log: Callable[[str], None] | None = None,
) -> MaterialSidecarExportResult:
    resolved_parent_root = parent_root.expanduser().resolve()
    package_root = resolve_mod_package_root(resolved_parent_root, package_info)
    if package_root.exists():
        resolved_package_root = package_root.resolve()
        if resolved_package_root == resolved_parent_root or resolved_parent_root not in resolved_package_root.parents:
            raise ValueError(f"Refusing to clear material sidecar package folder outside the export root: {resolved_package_root}")
        shutil.rmtree(resolved_package_root)
    package_root.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        if on_log is not None:
            on_log(message)

    written_files: list[Path] = []
    file_rows: list[MeshLooseModFile] = []
    written_virtual_paths: set[str] = set()

    edited_payload_path = normalize_mod_package_payload_path(edited_entry.path).as_posix()
    edited_target = package_root.joinpath(*PurePosixPath(edited_payload_path).parts)
    edited_target.parent.mkdir(parents=True, exist_ok=True)
    edited_target.write_text(edited_text, encoding="utf-8")
    written_files.append(edited_target)
    written_virtual_paths.add(edited_payload_path.lower())
    file_rows.append(
        MeshLooseModFile(
            path=edited_payload_path,
            package_group=edited_entry.pamt_path.parent.name,
            format=PurePosixPath(edited_payload_path).suffix.lstrip(".").lower(),
            generated_from=edited_entry.path,
            note="Edited material sidecar generated from archive XML values.",
        )
    )
    log(f"Wrote edited material sidecar: {edited_payload_path}")

    for related_entry in related_entries:
        if related_entry.path == edited_entry.path:
            continue
        payload_path = normalize_mod_package_payload_path(related_entry.path).as_posix()
        if not payload_path or payload_path.lower() in written_virtual_paths:
            continue
        target_path = package_root.joinpath(*PurePosixPath(payload_path).parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(read_entry_bytes(related_entry))
        written_files.append(target_path)
        written_virtual_paths.add(payload_path.lower())
        file_rows.append(
            MeshLooseModFile(
                path=payload_path,
                package_group=related_entry.pamt_path.parent.name,
                format=PurePosixPath(payload_path).suffix.lstrip(".").lower(),
                generated_from=edited_entry.path,
                note="Related material companion copied from archive.",
            )
        )
        log(f"Copied related material file: {payload_path}")

    manifest_path = package_root / "material_sidecar_edits.json"
    manifest_path.write_text(
        json.dumps(
            {
                "edited_entry": edited_entry.path,
                "related_entries": [entry.path for entry in related_entries],
                "file_count": len(file_rows),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    metadata_files = write_mesh_loose_mod_package_metadata(
        package_root,
        package_info,
        assets=(),
        files=file_rows,
        include_paired_lod=False,
        export_options=export_options,
        create_no_encrypt_file=create_no_encrypt_file,
    )
    metadata_files.append(manifest_path)
    return MaterialSidecarExportResult(
        package_root=package_root,
        written_files=tuple(written_files),
        metadata_files=tuple(metadata_files),
    )
