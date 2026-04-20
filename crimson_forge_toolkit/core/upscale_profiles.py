from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from crimson_forge_toolkit.constants import (
    SUPPORTED_TEXCONV_FORMAT_CHOICES,
    UPSCALE_TEXTURE_PRESET_ALL,
    UPSCALE_TEXTURE_PRESET_BALANCED,
    UPSCALE_TEXTURE_PRESET_COLOR_UI,
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
)
from crimson_forge_toolkit.core.classification_registry import get_registered_texture_classification

_PATH_TEXTURE_TYPE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("ui", re.compile(r"(^|[/\\])(ui|hud|menu|cursor|button|font)([/\\]|_|-|$)", re.IGNORECASE)),
    ("impostor", re.compile(r"(?:^|[_/\\-])impostor(?:$|[_/\\-])", re.IGNORECASE)),
)

_STEM_TEXTURE_TYPE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("normal", re.compile(r"(?:^|[_-])(wn|n|normal|nrm|norm|normalmap)(?:$|[_-])", re.IGNORECASE)),
    (
        "vector",
        re.compile(
            r"(?:^|[_-])(xvector|yvector|zvector|vector|flow|velocity|pivotpainter|pivotpos|pivot|position|pos|dr|op)(?:$|[_-])",
            re.IGNORECASE,
        ),
    ),
    (
        "height",
        re.compile(
            r"(?:^|[_-])(height|hgt|disp|displacement|dmap|bump|parallax|pom|ssdm)(?:$|[_-])",
            re.IGNORECASE,
        ),
    ),
    ("roughness", re.compile(r"(?:^|[_-])(roughness|gloss|glossiness|smoothness)(?:$|[_-])", re.IGNORECASE)),
    (
        "mask",
        re.compile(
            r"(?:^|[_-])(ma|mg|m|mask|masks|orm|rma|arm|ao|opacity|alpha|1bit|grayscale|metal|metallic|spec|specular|sp|o|subsurface|emi|d)(?:$|[_-])",
            re.IGNORECASE,
        ),
    ),
    ("emissive", re.compile(r"(?:^|[_-])(emc|emissive|glow|emit|em)(?:$|[_-])", re.IGNORECASE)),
    ("color", re.compile(r"(?:^|[_-])(diff|diffuse|albedo|alb|basecolor|base_color|color|col)(?:$|[_-])", re.IGNORECASE)),
)

_COLOR_INFIX_PATTERN = re.compile(r"[_-]cd(?:$|[_-])", re.IGNORECASE)

_EXACT_STEM_TEXTURE_TYPE_OVERRIDES: Dict[str, str] = {
    "snownormal": "normal",
    "snowmask": "mask",
    "nonetexturespecular": "mask",
}

_GROUP_SUFFIX_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"_(?:cd|dif|diff|color|col|albedo|basecolor|base_color)$", re.IGNORECASE),
    re.compile(r"_d$", re.IGNORECASE),
    re.compile(r"_(?:wn|n|nm|nrm|normal|normalmap)$", re.IGNORECASE),
    re.compile(r"_(?:xvector|yvector|zvector|vector|pivotpos|pivot|position|pos|flow|velocity|dr|op)$", re.IGNORECASE),
    re.compile(r"_(?:height|hgt|disp|displacement|dmap|bump|parallax|pom|ssdm|depth)$", re.IGNORECASE),
    re.compile(r"_(?:mask_1bit)$", re.IGNORECASE),
    re.compile(r"_(?:1bit)$", re.IGNORECASE),
    re.compile(r"_(?:mask_amg)$", re.IGNORECASE),
    re.compile(r"_(?:ct)$", re.IGNORECASE),
    re.compile(r"_(?:sp|spec|specular|gloss|gls)$", re.IGNORECASE),
    re.compile(r"_(?:ma|mg|m|mask|masks|orm|mra|rma|arm|ao|o|metal|metallic)$", re.IGNORECASE),
    re.compile(r"_(?:rough|roughness|rgh|smooth|smoothness)$", re.IGNORECASE),
    re.compile(r"_(?:em|emi|emc|emissive|glow|illum)$", re.IGNORECASE),
    re.compile(r"_(?:subsurface)$", re.IGNORECASE),
    re.compile(r"_(?:materials?|material|mat)$", re.IGNORECASE),
    re.compile(r"(?<=\d)[a-z]$", re.IGNORECASE),
)

_SIDECARE_EXTENSIONS = {".xml", ".material", ".shader", ".json", ".pami"}

_PRESET_UPSCALE_TYPES: Dict[str, Tuple[str, ...]] = {
    UPSCALE_TEXTURE_PRESET_BALANCED: ("color", "ui", "emissive", "impostor"),
    UPSCALE_TEXTURE_PRESET_COLOR_UI: ("color", "ui"),
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE: ("color", "ui", "emissive", "impostor"),
    UPSCALE_TEXTURE_PRESET_ALL: ("color", "ui", "emissive", "impostor", "normal", "roughness", "mask", "height", "vector", "unknown"),
}

_PRESET_DESCRIPTIONS: Dict[str, str] = {
    UPSCALE_TEXTURE_PRESET_BALANCED: "Recommended first test. Upscale visible color/UI-style maps only; leave normals, masks, grayscale technical maps, vectors, and unknown maps unchanged.",
    UPSCALE_TEXTURE_PRESET_COLOR_UI: "Safer visible-only preset. Upscale color and UI textures only; leave technical maps unchanged.",
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE: "Upscale color, UI, emissive, and impostor textures; leave technical maps unchanged.",
    UPSCALE_TEXTURE_PRESET_ALL: "Advanced/debug preset. Broadens eligibility to almost every image-like file, but planner/backend safety can still preserve technical maps unless you explicitly force an unsafe override.",
}

_ALL_TEXTURE_TYPES: Tuple[str, ...] = (
    "color",
    "ui",
    "emissive",
    "impostor",
    "normal",
    "height",
    "vector",
    "roughness",
    "mask",
    "unknown",
)

_TECHNICAL_TEXTURE_TYPES = frozenset({"normal", "roughness", "mask", "height", "vector"})
_LOSSY_PNG_RISK_TYPES = frozenset({"height", "vector", "roughness", "mask"})


@dataclass(slots=True, frozen=True)
class TextureSidecarBinding:
    texture_path: str
    parameter_name: str = ""
    submesh_name: str = ""
    sidecar_path: str = ""


@dataclass(slots=True)
class TexturePresetDefinition:
    preset: str
    label: str
    description: str
    upscale_types: Tuple[str, ...]
    copy_types: Tuple[str, ...]
    warning: str = ""


@dataclass(slots=True)
class TextureUpscaleDecision:
    path: str
    texture_type: str
    semantic_subtype: str
    semantic_confidence: int
    should_upscale: bool
    recommended_colorspace: str
    format_strategy: str
    recommended_texconv_format: str
    preserve_alpha: bool
    alpha_mode: str
    packed_channels: Tuple[str, ...] = ()
    precision_sensitive: bool = False
    preserve_original_due_to_intermediate: bool = False
    intermediate_policy: str = "png_ok"
    source_evidence: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TextureSetBundle:
    group_key: str
    root_name: str
    members: List[str] = field(default_factory=list)
    texture_types: List[str] = field(default_factory=list)
    package_labels: List[str] = field(default_factory=list)
    sidecar_count: int = 0


@dataclass(slots=True)
class LooseTreeCopyResult:
    source_root: Path
    destination_root: Path
    total_files: int
    copied_files: int
    skipped_files: int
    overwritten_files: int
    created_dirs: int
    failed_files: int
    copied_paths: List[str] = field(default_factory=list)
    skipped_paths: List[str] = field(default_factory=list)
    failed_paths: List[str] = field(default_factory=list)


@dataclass(slots=True)
class NcnnRetryPlan:
    requested_tile_size: int
    candidate_tile_sizes: Tuple[int, ...]


@dataclass(slots=True)
class TextureSemanticProfile:
    path: str
    texture_type: str
    semantic_subtype: str
    confidence: int
    alpha_mode: str
    packed_channels: Tuple[str, ...] = ()
    evidence: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TexturePreviewSample:
    mean_r: float
    mean_g: float
    mean_b: float
    mean_a: float
    luma_mean: float
    luma_range: float
    mean_chroma: float
    opaque_fraction: float
    transparent_fraction: float


def _strip_texture_sidecar_xml_namespace(tag: str) -> str:
    text = str(tag or "").strip()
    if "}" in text:
        return text.rsplit("}", 1)[-1]
    return text


def normalize_texture_reference_for_sidecar_lookup(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    normalized = re.sub(r"/{2,}", "/", normalized)
    return normalized.lower()


def _humanize_texture_parameter_name(parameter_name: str) -> str:
    raw_text = str(parameter_name or "").strip().lstrip("_")
    if not raw_text:
        return ""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_text)
    spaced = re.sub(r"[_\s]+", " ", spaced).strip()
    if not spaced:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in spaced.split())


@lru_cache(maxsize=512)
def _parse_texture_sidecar_bindings_cached(
    normalized_sidecar_text: str,
    sidecar_path: str,
) -> Tuple[TextureSidecarBinding, ...]:
    sidecar_text = str(normalized_sidecar_text or "").replace("\ufeff", "").replace("\x00", "").strip()
    if not sidecar_text:
        return ()
    sidecar_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", sidecar_text, count=1, flags=re.IGNORECASE)
    wrapped_text = f"<Root>{sidecar_text}</Root>"
    try:
        root = ET.fromstring(wrapped_text)
    except ET.ParseError:
        return ()

    bindings: List[TextureSidecarBinding] = []
    seen: set[Tuple[str, str, str]] = set()

    def _parameter_name_for(parameter: ET.Element) -> str:
        return str(
            parameter.attrib.get("_name")
            or parameter.attrib.get("StringItemID")
            or parameter.attrib.get("Name")
            or parameter.attrib.get("name")
            or ""
        ).strip()

    def _iter_texture_paths(parameter: ET.Element) -> Iterable[str]:
        direct_value = str(
            parameter.attrib.get("Value")
            or parameter.attrib.get("_value")
            or parameter.attrib.get("value")
            or ""
        ).strip()
        if direct_value:
            yield direct_value.replace("\\", "/")
        for resource in parameter.iter():
            if resource is parameter:
                continue
            if _strip_texture_sidecar_xml_namespace(resource.tag) != "ResourceReferencePath_ITexture":
                continue
            for attr_name in ("_path", "path", "_value", "Value", "value"):
                texture_path = str(resource.attrib.get(attr_name) or "").strip()
                if texture_path:
                    yield texture_path.replace("\\", "/")
                    break

    def _append_parameter_bindings(parameter: ET.Element, *, submesh_name: str = "") -> None:
        if _strip_texture_sidecar_xml_namespace(parameter.tag) != "MaterialParameterTexture":
            return
        parameter_name = _parameter_name_for(parameter)
        for texture_path in _iter_texture_paths(parameter):
            normalized_texture = normalize_texture_reference_for_sidecar_lookup(texture_path)
            if not normalized_texture:
                continue
            key = (
                normalized_texture,
                str(submesh_name or "").strip().lower(),
                str(parameter_name or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            bindings.append(
                TextureSidecarBinding(
                    texture_path=texture_path,
                    parameter_name=parameter_name,
                    submesh_name=submesh_name,
                    sidecar_path=sidecar_path,
                )
            )

    wrapper_count = 0
    for wrapper in root.iter():
        if _strip_texture_sidecar_xml_namespace(wrapper.tag) != "SkinnedMeshMaterialWrapper":
            continue
        wrapper_count += 1
        submesh_name = str(
            wrapper.attrib.get("_subMeshName")
            or wrapper.attrib.get("subMeshName")
            or wrapper.attrib.get("Name")
            or wrapper.attrib.get("name")
            or ""
        ).strip()
        for parameter in wrapper.iter():
            _append_parameter_bindings(parameter, submesh_name=submesh_name)

    if bindings or wrapper_count > 0:
        return tuple(bindings)

    for parameter in root.iter():
        _append_parameter_bindings(parameter)
    return tuple(bindings)


def parse_texture_sidecar_bindings(
    sidecar_text: str,
    *,
    sidecar_path: str = "",
) -> Tuple[TextureSidecarBinding, ...]:
    return _parse_texture_sidecar_bindings_cached(str(sidecar_text or ""), str(sidecar_path or ""))


def _semantic_priority_for_exact_sidecar_hint(texture_type: str) -> int:
    normalized = str(texture_type or "").strip().lower()
    return {
        "color": 6,
        "emissive": 6,
        "normal": 5,
        "height": 5,
        "vector": 5,
        "roughness": 4,
        "mask": 3,
    }.get(normalized, 0)


def _semantic_hint_from_sidecar_parameter(
    parameter_name: str,
) -> Optional[Tuple[str, str, int, Tuple[str, ...], Tuple[str, ...]]]:
    normalized = re.sub(r"[^a-z0-9]+", "", str(parameter_name or "").strip().lower())
    if not normalized:
        return None
    label = _humanize_texture_parameter_name(parameter_name) or str(parameter_name or "").strip()
    evidence = (f"sidecar exact binding: {label}",)

    if "emissive" in normalized or "glow" in normalized:
        return "emissive", "emissive", 99, evidence, ()
    if any(token in normalized for token in ("basecolor", "overlaycolor", "diffuse", "albedo", "colortexture")):
        semantic_subtype = "albedo" if any(token in normalized for token in ("basecolor", "albedo", "overlaycolor", "colortexture")) else "diffuse"
        return "color", semantic_subtype, 99, evidence, ()
    if "normal" in normalized:
        semantic_subtype = "world_normal" if "world" in normalized else "normal"
        return "normal", semantic_subtype, 99, evidence, ()
    if any(token in normalized for token in ("height", "displacement", "parallax", "pom", "ssdm", "bump")):
        if any(token in normalized for token in ("displacement", "ssdm")):
            semantic_subtype = "displacement"
        elif "bump" in normalized:
            semantic_subtype = "bump"
        elif any(token in normalized for token in ("parallax", "pom")):
            semantic_subtype = "parallax_height"
        else:
            semantic_subtype = "height"
        return "height", semantic_subtype, 98, evidence, ()
    if any(token in normalized for token in ("flow", "vector", "velocity", "position", "pivot")):
        if any(token in normalized for token in ("flow", "velocity")):
            semantic_subtype = "flow_vector"
        elif "pivot" in normalized:
            semantic_subtype = "pivot_position"
        elif "position" in normalized:
            semantic_subtype = "position_vector"
        else:
            semantic_subtype = "vector"
        return "vector", semantic_subtype, 96, evidence, ()
    if any(token in normalized for token in ("roughness", "gloss", "smoothness")):
        semantic_subtype = "gloss_or_smoothness" if any(token in normalized for token in ("gloss", "smoothness")) else "roughness"
        return "roughness", semantic_subtype, 97, evidence, ()
    if "specular" in normalized:
        return "mask", "specular", 97, evidence, ("specular",)
    if "metallic" in normalized or "metalness" in normalized:
        return "mask", "metallic", 97, evidence, ("metallic",)
    if "occlusion" in normalized or "ambientocclusion" in normalized or normalized.endswith("ao"):
        return "mask", "ao", 97, evidence, ("ao",)
    if "material" in normalized:
        return "mask", "packed_mask", 98, evidence, ()
    if "opacity" in normalized or "alpha" in normalized:
        return "mask", "opacity_mask", 98, evidence, ("alpha",)
    if "mask" in normalized:
        return "mask", "mask", 96, evidence, ()
    return None


def _select_exact_sidecar_semantic_hint(
    path_value: str,
    sidecar_texts: Sequence[str],
) -> Optional[Tuple[str, str, int, Tuple[str, ...], Tuple[str, ...]]]:
    normalized_target = normalize_texture_reference_for_sidecar_lookup(path_value)
    if not normalized_target:
        return None
    target_basename = PurePosixPath(normalized_target).name
    if not target_basename:
        return None

    best_hint: Optional[Tuple[str, str, int, Tuple[str, ...], Tuple[str, ...]]] = None
    best_score: Tuple[int, int, int, int] = (-1, -1, -1, -1)
    for sidecar_text in sidecar_texts:
        for binding in parse_texture_sidecar_bindings(sidecar_text):
            normalized_binding = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
            if not normalized_binding:
                continue
            if normalized_binding == normalized_target:
                path_score = 2
            elif PurePosixPath(normalized_binding).name == target_basename:
                path_score = 1
            else:
                continue
            semantic_hint = _semantic_hint_from_sidecar_parameter(binding.parameter_name)
            if semantic_hint is None:
                continue
            texture_type, semantic_subtype, confidence, evidence, packed_channels = semantic_hint
            candidate_score = (
                path_score,
                _semantic_priority_for_exact_sidecar_hint(texture_type),
                confidence,
                len(normalized_binding),
            )
            if candidate_score > best_score:
                best_score = candidate_score
                best_hint = (texture_type, semantic_subtype, confidence, evidence, packed_channels)
    return best_hint


def get_texture_preset_definition(preset: str) -> TexturePresetDefinition:
    normalized = str(preset or "").strip().lower()
    upscale_types = _PRESET_UPSCALE_TYPES.get(normalized, _PRESET_UPSCALE_TYPES[UPSCALE_TEXTURE_PRESET_BALANCED])
    if normalized == UPSCALE_TEXTURE_PRESET_COLOR_UI:
        label = "Color + UI only (safer)"
    elif normalized == UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE:
        label = "Color + UI + emissive"
    elif normalized == UPSCALE_TEXTURE_PRESET_ALL:
        label = "All textures (advanced)"
    else:
        label = "Balanced mixed textures (recommended)"
        normalized = UPSCALE_TEXTURE_PRESET_BALANCED
    copy_types = tuple(texture_type for texture_type in _ALL_TEXTURE_TYPES if texture_type not in upscale_types)
    warning = ""
    if normalized == UPSCALE_TEXTURE_PRESET_ALL:
        warning = (
            "This preset broadens technical-map eligibility, but unsafe technical upscaling still depends on planner/backend rules unless the expert override is enabled. "
            "Expect more failures, darker output, or broken shading unless you verify the results carefully."
        )
    return TexturePresetDefinition(
        preset=normalized,
        label=label,
        description=_PRESET_DESCRIPTIONS.get(normalized, _PRESET_DESCRIPTIONS[UPSCALE_TEXTURE_PRESET_BALANCED]),
        upscale_types=upscale_types,
        copy_types=copy_types,
        warning=warning,
    )


def describe_texture_preset(preset: str) -> str:
    return get_texture_preset_definition(preset).description


def classify_texture_type(path_value: str) -> str:
    registered = get_registered_texture_classification(path_value)
    if registered is not None:
        return str(registered.texture_type or "unknown").strip().lower() or "unknown"
    normalized = path_value.replace("\\", "/")
    lowered = normalized.lower()
    stem = PurePosixPath(normalized).stem.lower()
    exact_override = _EXACT_STEM_TEXTURE_TYPE_OVERRIDES.get(stem)
    if exact_override is not None:
        return exact_override
    if re.search(r"(?:^|[_-])ct$", stem, re.IGNORECASE):
        return "color"
    for texture_type, pattern in _PATH_TEXTURE_TYPE_PATTERNS:
        if pattern.search(lowered):
            return texture_type
    for texture_type, pattern in _STEM_TEXTURE_TYPE_PATTERNS:
        if pattern.search(stem):
            return texture_type
    if stem.endswith("normal"):
        return "normal"
    if stem.endswith("specular") or stem.endswith("mask"):
        return "mask"
    if _COLOR_INFIX_PATTERN.search(stem):
        return "color"
    return "unknown"


def should_upscale_texture(texture_type: str, preset: str) -> bool:
    definition = get_texture_preset_definition(preset)
    return texture_type in definition.upscale_types


def is_technical_texture_type(texture_type: str) -> bool:
    return texture_type in _TECHNICAL_TEXTURE_TYPES


def is_png_intermediate_high_risk(texture_type: str, original_texconv_format: str = "") -> bool:
    original_upper = str(original_texconv_format or "").strip().upper()
    if texture_type in _LOSSY_PNG_RISK_TYPES:
        return True
    if "FLOAT" in original_upper or "SNORM" in original_upper:
        return True
    return False


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _sorted_tuple(values: Iterable[str]) -> Tuple[str, ...]:
    unique = {value.strip().lower() for value in values if value and value.strip()}
    return tuple(sorted(unique))


def _path_stem(path_value: str) -> str:
    normalized = path_value.replace("\\", "/")
    return PurePosixPath(normalized).stem.lower()


def _stem_has_token(stem_value: str, *tokens: str) -> bool:
    for token in tokens:
        if re.search(rf"(?:^|[_-]){re.escape(token)}(?:$|[_-])", stem_value, re.IGNORECASE):
            return True
    return False


def _infer_family_semantics(
    path_value: str,
    *,
    family_members: Sequence[str],
) -> Optional[Tuple[str, str, int, str]]:
    current_normalized = path_value.replace("\\", "/").lower()
    current_stem = _path_stem(path_value)
    sibling_stems = {
        _path_stem(member)
        for member in family_members
        if str(member).replace("\\", "/").lower() != current_normalized
    }
    sibling_types = {
        classify_texture_type(member)
        for member in family_members
        if str(member).replace("\\", "/").lower() != current_normalized
    }
    sibling_types.discard("unknown")

    if re.search(r"(?:^|[_-])(sp|spec|specular)$", current_stem):
        return "mask", "specular", 84, "family-aware specular suffix"

    if re.search(r"(?:^|[_-])m$", current_stem) and sibling_types.intersection({"normal", "height", "roughness", "mask", "color", "emissive"}):
        semantic_subtype = "packed_mask" if sibling_types.intersection({"roughness", "mask"}) else "mask"
        return "mask", semantic_subtype, 72, "family-aware _m suffix beside related texture maps"

    if re.search(r"(?:^|[_-])ct$", current_stem) and sibling_types.intersection({"normal", "height", "roughness", "mask", "emissive"}):
        return "color", "albedo_variant", 70, "family-aware _ct variant beside related texture maps"

    relaxed_stem = re.sub(r"(?<=\d)[a-z]$", "", current_stem)
    if relaxed_stem != current_stem and (relaxed_stem in sibling_stems or sibling_types):
        return "color", "albedo_variant", 68, "family-aware trailing variant suffix"
    if relaxed_stem != current_stem:
        sibling_relaxed_stems = {
            re.sub(r"(?<=\d)[a-z]$", "", stem)
            for stem in sibling_stems
        }
        if relaxed_stem in sibling_relaxed_stems:
            return "color", "albedo_variant", 67, "family of trailing variant suffixes"

    trailing_variant_pattern = re.compile(rf"^{re.escape(current_stem)}[a-z]$", re.IGNORECASE)
    if any(trailing_variant_pattern.match(stem) for stem in sibling_stems):
        return "color", "albedo", 67, "family base file beside trailing variant suffixes"

    if sibling_types.intersection({"normal", "height", "roughness", "mask"}) and not re.search(
        r"(?:^|[_-])(m|sp|spec|specular)$",
        current_stem,
    ):
        return "color", "albedo", 66, "family contains technical companion maps"

    return None


def _infer_preview_semantics(
    preview_sample: TexturePreviewSample,
    *,
    original_texconv_format: str,
    has_alpha: bool,
    family_members: Sequence[str],
) -> Optional[Tuple[str, str, int, str]]:
    del has_alpha
    original_upper = original_texconv_format.strip().upper()
    sibling_types = {classify_texture_type(member) for member in family_members}
    sibling_types.discard("unknown")
    mean_rg = abs(preview_sample.mean_r - preview_sample.mean_g)
    mean_gb = abs(preview_sample.mean_g - preview_sample.mean_b)
    mean_rb = abs(preview_sample.mean_r - preview_sample.mean_b)
    max_mean_delta = max(mean_rg, mean_gb, mean_rb)
    blue_dominance = preview_sample.mean_b - max(preview_sample.mean_r, preview_sample.mean_g)

    if original_upper in {"BC5_UNORM", "BC5_SNORM"}:
        return "normal", "normal", 82, "BC5 source format is commonly used for normals"

    if blue_dominance >= 28.0 and preview_sample.mean_b >= 150.0 and preview_sample.opaque_fraction >= 0.95:
        return "normal", "normal", 78, "preview is strongly blue-dominant like a normal map"

    if preview_sample.mean_chroma <= 7.0 and preview_sample.luma_range >= 18.0:
        if original_upper.startswith("BC4") or original_upper.startswith("R8"):
            return "roughness", "roughness", 66, "preview is nearly grayscale and the source format is single-channel-like"
        return "mask", "grayscale_data", 62, "preview is nearly grayscale and looks like technical scalar data"

    if (
        original_upper.endswith("_SRGB")
        or sibling_types.intersection({"normal", "height", "roughness", "mask"})
        or preview_sample.transparent_fraction > 0.01
    ) and preview_sample.mean_chroma >= 12.0 and blue_dominance < 24.0 and max_mean_delta >= 8.0:
        return "color", "albedo", 70, "preview shows persistent color variation consistent with a visible color texture"

    return None


def infer_texture_semantics(
    path_value: str,
    *,
    sidecar_texts: Sequence[str] = (),
    original_texconv_format: str = "",
    has_alpha: bool = False,
    family_members: Sequence[str] = (),
    preview_sample: Optional[TexturePreviewSample] = None,
) -> TextureSemanticProfile:
    lowered = path_value.replace("\\", "/").lower()
    stem_lower = _path_stem(path_value)
    texture_type = classify_texture_type(path_value)
    semantic_subtype = texture_type
    confidence = 55 if texture_type == "unknown" else 72
    alpha_mode = "present" if has_alpha else "none"
    packed_channels: List[str] = []
    evidence: List[str] = []
    combined_sidecar_text = "\n".join(text.lower() for text in sidecar_texts if text).lower()
    original_upper = original_texconv_format.strip().upper()
    exact_sidecar_hint = _select_exact_sidecar_semantic_hint(path_value, sidecar_texts)

    if exact_sidecar_hint is not None:
        texture_type, semantic_subtype, confidence, exact_evidence, exact_packed_channels = exact_sidecar_hint
        evidence.extend(exact_evidence)
        packed_channels.extend(exact_packed_channels)
    elif texture_type == "height":
        semantic_subtype = "height"
        if _stem_has_token(stem_lower, "disp", "displacement", "dmap") or _contains_any(
            combined_sidecar_text,
            ("displacement", "displace", "vertex offset", "vertex_offset"),
        ):
            semantic_subtype = "displacement"
            confidence = 90
            evidence.append("displacement naming/material hint")
        elif _stem_has_token(stem_lower, "bump", "bmp") or _contains_any(combined_sidecar_text, ("bump", "bumpmap")):
            semantic_subtype = "bump"
            confidence = 88
            evidence.append("bump naming/material hint")
        elif _stem_has_token(stem_lower, "parallax", "pom", "ssdm") or _contains_any(
            combined_sidecar_text,
            ("parallax", "pom", "ssdm"),
        ):
            semantic_subtype = "parallax_height"
            confidence = 92
            evidence.append("parallax/POM/SSDM hint")
        else:
            confidence = 78
            evidence.append("generic height/displacement naming")
    elif texture_type == "vector":
        semantic_subtype = "vector"
        if _stem_has_token(stem_lower, "dr"):
            semantic_subtype = "direction_vector"
            confidence = 94
            evidence.append("direction-vector suffix")
        elif _stem_has_token(stem_lower, "op"):
            semantic_subtype = "effect_vector"
            confidence = 90
            evidence.append("effect/distortion vector suffix")
        elif _contains_any(lowered, ("pivotpainter",)):
            semantic_subtype = "pivot_position"
            confidence = 94
            evidence.append("pivot-painter naming")
        elif _contains_any(lowered, ("pivotpos", "pivot_pos")):
            semantic_subtype = "pivot_position"
            confidence = 96
            evidence.append("pivot-position naming")
        elif _contains_any(lowered, ("flow", "velocity")) or _contains_any(
            combined_sidecar_text,
            ("flow", "velocity"),
        ):
            semantic_subtype = "flow_vector"
            confidence = 92
            evidence.append("flow/velocity hint")
        elif _contains_any(lowered, ("position", "/pos", "_pos", "worldpos", "world_pos")) or _contains_any(
            combined_sidecar_text,
            ("position", "world position", "pivot"),
        ):
            semantic_subtype = "position_vector"
            confidence = 90
            evidence.append("position/vector hint")
        else:
            confidence = 82
            evidence.append("generic vector naming")
    elif texture_type == "mask":
        semantic_subtype = "mask"
        sibling_types = {
            classify_texture_type(member)
            for member in family_members
            if str(member).replace("\\", "/").lower() != lowered
        }
        sibling_types.discard("unknown")
        if _contains_any(lowered, ("_orm", "/orm", "-orm")) or _contains_any(combined_sidecar_text, ("orm", "occlusion roughness metallic")):
            semantic_subtype = "orm"
            packed_channels.extend(("ao", "roughness", "metallic"))
            confidence = 95
            evidence.append("ORM packed-map hint")
        elif _contains_any(lowered, ("_rma", "/rma", "-rma")) or _contains_any(combined_sidecar_text, ("rma", "roughness metallic ao")):
            semantic_subtype = "rma"
            packed_channels.extend(("roughness", "metallic", "ao"))
            confidence = 95
            evidence.append("RMA packed-map hint")
        elif _contains_any(lowered, ("_mra", "/mra", "-mra")) or _contains_any(combined_sidecar_text, ("mra", "metallic roughness ao")):
            semantic_subtype = "mra"
            packed_channels.extend(("metallic", "roughness", "ao"))
            confidence = 95
            evidence.append("MRA packed-map hint")
        elif _contains_any(lowered, ("_arm", "/arm", "-arm")) or _contains_any(combined_sidecar_text, ("arm", "ao roughness metallic")):
            semantic_subtype = "arm"
            packed_channels.extend(("ao", "roughness", "metallic"))
            confidence = 95
            evidence.append("ARM packed-map hint")
        elif _stem_has_token(stem_lower, "sp", "spec", "specular"):
            semantic_subtype = "specular"
            packed_channels.append("specular")
            confidence = 90
            evidence.append("specular suffix")
        elif _stem_has_token(stem_lower, "ma"):
            semantic_subtype = "material_mask"
            confidence = 92
            evidence.append("material-mask suffix")
        elif _stem_has_token(stem_lower, "mg"):
            semantic_subtype = "material_response"
            confidence = 90
            evidence.append("material-response suffix")
        elif _stem_has_token(stem_lower, "m"):
            semantic_subtype = "packed_mask" if sibling_types.intersection({"roughness", "mask"}) else "mask"
            confidence = 80 if semantic_subtype == "packed_mask" else 76
            evidence.append("family-aware _m mask suffix")
        elif _stem_has_token(stem_lower, "subsurface") or _contains_any(combined_sidecar_text, ("subsurface", "sss")):
            semantic_subtype = "subsurface"
            confidence = 90
            evidence.append("subsurface/SSS hint")
        elif _stem_has_token(stem_lower, "emi"):
            semantic_subtype = "emissive_intensity"
            confidence = 88
            evidence.append("emissive-intensity suffix")
        elif _stem_has_token(stem_lower, "o"):
            semantic_subtype = "ao"
            packed_channels.append("ao")
            confidence = 90
            evidence.append("occlusion suffix")
        elif _contains_any(lowered, ("ao", "occlusion")) or _contains_any(combined_sidecar_text, ("ambient occlusion", "occlusion")):
            semantic_subtype = "ao"
            packed_channels.append("ao")
            confidence = 88
            evidence.append("ambient-occlusion hint")
        elif _contains_any(lowered, ("metal", "metallic")) or _contains_any(combined_sidecar_text, ("metallic", "metalness")):
            semantic_subtype = "metallic"
            packed_channels.append("metallic")
            confidence = 88
            evidence.append("metallic hint")
        elif _contains_any(lowered, ("spec", "specular")) or _contains_any(combined_sidecar_text, ("specular", "gloss")):
            semantic_subtype = "specular"
            packed_channels.append("specular")
            confidence = 88
            evidence.append("specular/gloss hint")
        elif _contains_any(lowered, ("opacity", "alpha", "1bit", "cutout")) or _contains_any(
            combined_sidecar_text,
            ("opacity", "alpha", "alpha mask", "cutout"),
        ):
            semantic_subtype = "opacity_mask"
            packed_channels.append("alpha")
            confidence = 90
            evidence.append("opacity/alpha mask hint")
        elif _contains_any(lowered, ("depth_grayscale", "grayscale")):
            semantic_subtype = "grayscale_data"
            confidence = 86
            evidence.append("grayscale scalar-data hint")
        elif _stem_has_token(stem_lower, "d"):
            semantic_subtype = "detail_support"
            confidence = 74
            evidence.append("grayscale support/detail suffix")
        else:
            confidence = 78
            evidence.append("generic mask naming")
    elif texture_type == "roughness":
        semantic_subtype = "roughness"
        if _contains_any(lowered, ("gloss", "smooth")) or _contains_any(combined_sidecar_text, ("gloss", "smoothness")):
            semantic_subtype = "gloss_or_smoothness"
            confidence = 86
            evidence.append("gloss/smoothness hint")
        else:
            confidence = 80
            evidence.append("roughness naming")
    elif texture_type == "color":
        semantic_subtype = "albedo" if (_contains_any(lowered, ("albedo", "basecolor", "base_color")) or _stem_has_token(stem_lower, "color", "albedo", "basecolor", "base_color", "col") or _COLOR_INFIX_PATTERN.search(stem_lower)) else "diffuse"
        confidence = 84
        evidence.append("color/albedo naming")
    elif texture_type == "normal":
        semantic_subtype = "world_normal" if _stem_has_token(stem_lower, "wn") else "normal"
        confidence = 96
        evidence.append("normal-map naming")
    elif texture_type == "emissive":
        semantic_subtype = "emissive_color" if _stem_has_token(stem_lower, "emc") else "emissive"
        confidence = 90
        evidence.append("emissive/glow naming")
    elif texture_type == "ui":
        semantic_subtype = "ui"
        confidence = 92
        evidence.append("UI naming/folder hint")
    elif texture_type == "impostor":
        semantic_subtype = "impostor"
        confidence = 92
        evidence.append("impostor naming")

    if has_alpha:
        if _contains_any(lowered, ("cutout", "clip", "alphatest", "alpha_test", "foliage", "holdout", "1bit")) or _contains_any(
            combined_sidecar_text,
            (
                "cutout",
                "alpha test",
                "alphatest",
                "clip(",
                "clip ",
                "keep coverage",
                "alpha coverage",
                "holdout",
                "alpha_to_coverage",
                "alpha to coverage",
            ),
        ):
            alpha_mode = "cutout"
            confidence = max(confidence, 88)
            evidence.append("alpha-test/cutout hint")
        elif _contains_any(combined_sidecar_text, ("premult", "premul", "premultiplied", "premultiplied alpha")):
            alpha_mode = "premultiplied"
            confidence = max(confidence, 84)
            evidence.append("premultiplied-alpha hint")
        elif semantic_subtype in {
            "orm",
            "rma",
            "mra",
            "arm",
            "packed_mask",
            "opacity_mask",
            "material_mask",
            "material_response",
            "ao",
            "metallic",
            "specular",
            "detail_support",
            "subsurface",
            "emissive_intensity",
            "grayscale_data",
        }:
            alpha_mode = "channel_data"
            evidence.append("alpha channel treated as data, not transparency")
        else:
            alpha_mode = "straight"

    if combined_sidecar_text and texture_type == "unknown":
        if _contains_any(combined_sidecar_text, ("normal", "normalmap")):
            texture_type = "normal"
            semantic_subtype = "normal"
            confidence = 74
            evidence.append("sidecar normal hint")
        elif _contains_any(combined_sidecar_text, ("roughness", "gloss", "smoothness")):
            texture_type = "roughness"
            semantic_subtype = "roughness"
            confidence = 72
            evidence.append("sidecar roughness hint")
        elif _contains_any(combined_sidecar_text, ("metallic", "specular", "ao", "occlusion", "mask")):
            texture_type = "mask"
            semantic_subtype = "packed_mask"
            confidence = 72
            evidence.append("sidecar packed-mask hint")
        elif _contains_any(combined_sidecar_text, ("height", "displacement", "bump", "parallax", "pom", "ssdm")):
            texture_type = "height"
            if _contains_any(combined_sidecar_text, ("displacement", "displace", "vertex offset", "vertex_offset")):
                semantic_subtype = "displacement"
                confidence = 80
                evidence.append("sidecar displacement hint")
            elif _contains_any(combined_sidecar_text, ("bump", "bumpmap")):
                semantic_subtype = "bump"
                confidence = 78
                evidence.append("sidecar bump hint")
            elif _contains_any(combined_sidecar_text, ("parallax", "pom", "ssdm")):
                semantic_subtype = "parallax_height"
                confidence = 82
                evidence.append("sidecar parallax/POM/SSDM hint")
            else:
                semantic_subtype = "height"
                confidence = 74
                evidence.append("sidecar height hint")
        elif _contains_any(combined_sidecar_text, ("basecolor", "albedo", "diffuse", "emissive")):
            texture_type = "color"
            semantic_subtype = "albedo"
            confidence = 70
            evidence.append("sidecar color hint")

    if texture_type == "unknown":
        if original_upper.endswith("_SRGB"):
            texture_type = "color"
            semantic_subtype = "albedo"
            confidence = max(confidence, 68)
            evidence.append(f"sRGB source format {original_upper}")
        elif original_upper in {"BC5_UNORM", "BC5_SNORM"}:
            texture_type = "normal"
            semantic_subtype = "normal"
            confidence = max(confidence, 78)
            evidence.append(f"BC5 source format {original_upper}")
        elif original_upper.startswith("BC4") or original_upper.startswith("R8"):
            texture_type = "mask"
            semantic_subtype = "grayscale_data"
            confidence = max(confidence, 62)
            evidence.append(f"single-channel-like source format {original_upper}")

    if texture_type == "unknown" and family_members:
        family_hint = _infer_family_semantics(path_value, family_members=family_members)
        if family_hint is not None:
            texture_type, semantic_subtype, confidence, reason = family_hint
            evidence.append(reason)

    if texture_type == "unknown" and preview_sample is not None:
        preview_hint = _infer_preview_semantics(
            preview_sample,
            original_texconv_format=original_texconv_format,
            has_alpha=has_alpha,
            family_members=family_members,
        )
        if preview_hint is not None:
            texture_type, semantic_subtype, confidence, reason = preview_hint
            evidence.append(reason)

    if "FLOAT" in original_upper or "SNORM" in original_upper:
        evidence.append(f"precision-sensitive format {original_upper}")
        confidence = max(confidence, 90)

    registered = get_registered_texture_classification(path_value)
    if registered is not None:
        texture_type = str(registered.texture_type or texture_type).strip().lower() or texture_type
        semantic_subtype = str(registered.semantic_subtype or texture_type).strip().lower() or texture_type
        confidence = 100
        evidence = [
            f"user classification registry: {texture_type}/{semantic_subtype}",
            *[item for item in evidence if not item.startswith("user classification registry:")],
        ]
        if texture_type in {"color", "ui", "emissive", "impostor", "normal", "roughness", "height", "vector"}:
            packed_channels = []

    return TextureSemanticProfile(
        path=path_value,
        texture_type=texture_type,
        semantic_subtype=semantic_subtype,
        confidence=confidence,
        alpha_mode=alpha_mode,
        packed_channels=_sorted_tuple(packed_channels),
        evidence=evidence,
    )


def suggest_texture_upscale_decision(
    path_value: str,
    *,
    preset: str = UPSCALE_TEXTURE_PRESET_BALANCED,
    original_texconv_format: str = "",
    has_alpha: bool = False,
    sidecar_texts: Sequence[str] = (),
    enable_automatic_rules: bool = True,
    family_members: Sequence[str] = (),
    preview_sample: Optional[TexturePreviewSample] = None,
) -> TextureUpscaleDecision:
    semantic = infer_texture_semantics(
        path_value,
        sidecar_texts=sidecar_texts,
        original_texconv_format=original_texconv_format,
        has_alpha=has_alpha,
        family_members=family_members,
        preview_sample=preview_sample,
    )
    texture_type = semantic.texture_type
    should_upscale = should_upscale_texture(texture_type, preset)
    notes: List[str] = []
    color_space = "unknown"
    format_strategy = "match_original"
    recommended_texconv_format = original_texconv_format.strip().upper()
    preserve_alpha = has_alpha
    preserve_original_due_to_intermediate = False
    intermediate_policy = "png_ok"
    precision_sensitive = False
    source_evidence = list(semantic.evidence)

    if texture_type in {"color", "ui", "emissive", "impostor"}:
        color_space = "srgb"
        format_strategy = "bc7_srgb"
        recommended_texconv_format = "BC7_UNORM_SRGB"
        notes.append("Treat as color data and keep sRGB handling enabled.")
        if texture_type == "ui":
            notes.append("UI textures should avoid linear-color conversion.")
    elif texture_type == "normal":
        color_space = "linear"
        if has_alpha:
            format_strategy = "normal_with_alpha_linear"
            recommended_texconv_format = "BC7_UNORM"
            preserve_alpha = True
            notes.append("Normal map appears to use alpha, so an alpha-capable linear format is safer than BC5.")
        else:
            format_strategy = "bc5_linear"
            recommended_texconv_format = "BC5_UNORM"
            preserve_alpha = False
            notes.append("Normal maps should stay linear and usually compress to BC5.")
    elif texture_type == "height":
        color_space = "linear"
        format_strategy = "preserve_linear_scalar"
        if original_texconv_format.strip().upper().startswith("R") and "FLOAT" in original_texconv_format.strip().upper():
            recommended_texconv_format = original_texconv_format.strip().upper()
        else:
            recommended_texconv_format = "BC4_UNORM"
        preserve_alpha = False
        notes.append(f"{semantic.semantic_subtype.replace('_', ' ')} maps are technical grayscale data and should stay linear.")
        notes.append("PNG intermediates can lose precision for these maps, so safer presets leave them unchanged.")
    elif texture_type == "vector":
        color_space = "linear"
        format_strategy = "preserve_vector_precision"
        recommended_texconv_format = original_texconv_format.strip().upper() or "BC5_UNORM"
        preserve_alpha = False
        notes.append(f"{semantic.semantic_subtype.replace('_', ' ')} maps often store signed or high-precision data and should stay linear.")
        notes.append("PNG intermediates can quantize vector data, so safer presets leave them unchanged.")
    elif texture_type == "roughness":
        color_space = "linear"
        format_strategy = "bc4_linear"
        recommended_texconv_format = "BC4_UNORM"
        preserve_alpha = False
        notes.append("Roughness/gloss maps are usually safest as single-channel linear data.")
    elif texture_type == "mask":
        color_space = "linear"
        if semantic.semantic_subtype in {"orm", "rma", "mra", "arm", "packed_mask"}:
            format_strategy = "preserve_packed_channels"
            recommended_texconv_format = original_texconv_format.strip().upper() or ("BC7_UNORM" if has_alpha else "BC1_UNORM")
            preserve_alpha = has_alpha
            notes.append("Packed channel maps should preserve exact channel meaning and stay linear.")
        elif semantic.semantic_subtype == "opacity_mask":
            format_strategy = "alpha_mask_linear"
            recommended_texconv_format = "BC7_UNORM" if has_alpha else "BC4_UNORM"
            preserve_alpha = has_alpha
            notes.append("Opacity/alpha masks should stay linear and preserve alpha semantics.")
        else:
            format_strategy = "bc7_linear" if has_alpha else "bc4_linear"
            recommended_texconv_format = "BC7_UNORM" if has_alpha else "BC4_UNORM"
            notes.append("Packed or mask maps should stay linear; keep alpha if the source uses it.")
    else:
        if recommended_texconv_format not in SUPPORTED_TEXCONV_FORMAT_CHOICES:
            recommended_texconv_format = original_texconv_format.strip().upper() or "MATCH_ORIGINAL"
        notes.append("Unknown textures should be reviewed before forcing a new format.")

    if semantic.alpha_mode == "cutout":
        preserve_alpha = True
        notes.append("Alpha-tested/cutout texture detected; alpha-aware mip handling is recommended.")
    elif semantic.alpha_mode == "premultiplied":
        preserve_alpha = True
        notes.append("Possible premultiplied-alpha texture detected; verify blend behavior after rebuild.")
    elif semantic.alpha_mode == "channel_data":
        notes.append("Alpha appears to be channel data rather than transparency; separate-alpha mip handling may be safer.")

    if original_texconv_format:
        original_upper = original_texconv_format.strip().upper()
        if "FLOAT" in original_upper or "SNORM" in original_upper:
            precision_sensitive = True
        if original_upper in SUPPORTED_TEXCONV_FORMAT_CHOICES and original_upper != recommended_texconv_format:
            notes.append(f"Source format is {original_upper}; compare it against the suggested output format before changing it.")
        elif texture_type in {"height", "vector"} and original_upper and original_upper != recommended_texconv_format:
            notes.append(f"Source format is {original_upper}; preserve it if this map carries precision-sensitive technical data.")

    if enable_automatic_rules:
        original_upper = original_texconv_format.strip().upper()
        if "FLOAT" in original_upper or "SNORM" in original_upper:
            precision_sensitive = True
            preserve_original_due_to_intermediate = True
            intermediate_policy = "preserve_original"
            notes.append("Automatic rules will preserve the original DDS because the source format is precision-sensitive.")
        elif texture_type == "vector":
            preserve_original_due_to_intermediate = True
            intermediate_policy = "preserve_original"
            notes.append("Automatic rules will preserve the original DDS for vector-style technical maps.")
        elif texture_type == "height":
            preserve_original_due_to_intermediate = True
            intermediate_policy = "preserve_original"
            notes.append("Automatic rules will preserve the original DDS for grayscale height/displacement support maps.")
        elif texture_type == "roughness":
            preserve_original_due_to_intermediate = True
            intermediate_policy = "preserve_original"
            notes.append("Automatic rules will preserve the original DDS for roughness/gloss-style scalar maps.")
        elif texture_type == "mask":
            preserve_original_due_to_intermediate = True
            intermediate_policy = "preserve_original"
            notes.append("Automatic rules will preserve the original DDS for mask, support, and packed-channel maps.")
        elif is_png_intermediate_high_risk(texture_type, original_texconv_format):
            intermediate_policy = "risky_png"
            notes.append("PNG intermediates are risky for this texture type or source format.")

    if semantic.alpha_mode == "cutout" and intermediate_policy == "png_ok":
        intermediate_policy = "risky_png"
    if source_evidence:
        notes.append("semantic evidence: " + "; ".join(source_evidence[:4]))

    return TextureUpscaleDecision(
        path=path_value,
        texture_type=texture_type,
        semantic_subtype=semantic.semantic_subtype,
        semantic_confidence=semantic.confidence,
        should_upscale=should_upscale,
        recommended_colorspace=color_space,
        format_strategy=format_strategy,
        recommended_texconv_format=recommended_texconv_format,
        preserve_alpha=preserve_alpha,
        alpha_mode=semantic.alpha_mode,
        packed_channels=semantic.packed_channels,
        precision_sensitive=precision_sensitive,
        preserve_original_due_to_intermediate=preserve_original_due_to_intermediate,
        intermediate_policy=intermediate_policy,
        source_evidence=source_evidence,
        notes=notes,
    )


def build_texture_upscale_decisions(
    paths: Sequence[str | Path],
    *,
    preset: str = UPSCALE_TEXTURE_PRESET_BALANCED,
    original_texconv_format: str = "",
) -> List[TextureUpscaleDecision]:
    return [
        suggest_texture_upscale_decision(str(path), preset=preset, original_texconv_format=original_texconv_format)
        for path in paths
    ]


def build_ncnn_retry_tile_candidates(
    tile_size: int,
    *,
    minimum_tile_size: int = 32,
    include_full_frame_fallback: bool = False,
) -> NcnnRetryPlan:
    requested = max(0, int(tile_size))
    minimum = max(1, int(minimum_tile_size))
    candidates: List[int] = []
    if requested == 0:
        for candidate in (512, 256, 128, 64, 32):
            if candidate >= minimum and candidate not in candidates:
                candidates.append(candidate)
        return NcnnRetryPlan(requested_tile_size=requested, candidate_tile_sizes=tuple(candidates))

    current = max(0, requested // 2)
    while current >= minimum:
        if current not in candidates:
            candidates.append(current)
        if current == minimum:
            break
        next_value = max(minimum, current // 2)
        if next_value == current:
            break
        current = next_value

    if requested > 0 and include_full_frame_fallback and 0 not in candidates:
        candidates.append(0)

    return NcnnRetryPlan(requested_tile_size=requested, candidate_tile_sizes=tuple(candidates))


def _strip_family_suffix(stem: str) -> str:
    candidate = stem
    changed = True
    while changed:
        changed = False
        for pattern in _GROUP_SUFFIX_PATTERNS:
            updated = pattern.sub("", candidate)
            if updated != candidate:
                candidate = updated
                changed = True
    candidate = candidate.rstrip("._- ")
    return candidate or stem


def derive_texture_group_key(path_value: str) -> str:
    normalized = path_value.replace("\\", "/")
    if "/" in normalized:
        folder, filename = normalized.rsplit("/", 1)
    else:
        folder, filename = "", normalized
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    extension = f".{filename.rsplit('.', 1)[1].lower()}" if "." in filename else ""
    if extension in _SIDECARE_EXTENSIONS:
        return f"{folder}/{stem}" if folder else stem
    family = _strip_family_suffix(stem)
    return f"{folder}/{family}" if folder else family


def group_texture_paths(paths: Sequence[str | Path]) -> List[TextureSetBundle]:
    grouped: Dict[str, TextureSetBundle] = {}
    for value in paths:
        path_text = str(value).replace("\\", "/")
        group_key = derive_texture_group_key(path_text)
        if "/" in path_text:
            folder, filename = path_text.rsplit("/", 1)
        else:
            folder, filename = "", path_text
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        extension = f".{filename.rsplit('.', 1)[1].lower()}" if "." in filename else ""
        texture_type = classify_texture_type(path_text) if extension not in _SIDECARE_EXTENSIONS else "sidecar"
        bundle = grouped.setdefault(
            group_key,
            TextureSetBundle(
                group_key=group_key,
                root_name=group_key.rsplit("/", 1)[-1],
            ),
        )
        bundle.members.append(path_text)
        bundle.texture_types.append(texture_type)
        if folder:
            package_label = folder.split("/", 1)[0]
            if package_label and package_label not in bundle.package_labels:
                bundle.package_labels.append(package_label)
        if extension in _SIDECARE_EXTENSIONS:
            bundle.sidecar_count += 1
    bundles = sorted(grouped.values(), key=lambda item: (item.group_key.lower(), item.root_name.lower()))
    for bundle in bundles:
        bundle.members.sort(key=str.lower)
        bundle.texture_types = list(dict.fromkeys(bundle.texture_types))
        bundle.package_labels.sort(key=str.lower)
    return bundles


def copy_loose_tree_preserving_paths(
    source_root: Path,
    destination_root: Path,
    *,
    selected_paths: Optional[Sequence[Path | str]] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> LooseTreeCopyResult:
    resolved_source = Path(source_root)
    resolved_destination = Path(destination_root)
    if not resolved_source.exists() or not resolved_source.is_dir():
        raise ValueError(f"Source root does not exist: {resolved_source}")

    if selected_paths is None:
        source_files = [path for path in resolved_source.rglob("*") if path.is_file()]
    else:
        source_files = []
        for entry in selected_paths:
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = resolved_source / candidate
            source_files.append(candidate)

    created_dirs: set[Path] = set()
    copied_files = 0
    skipped_files = 0
    overwritten_files = 0
    failed_files = 0
    copied_paths: List[str] = []
    skipped_paths: List[str] = []
    failed_paths: List[str] = []

    total = len(source_files)
    for index, source_file in enumerate(source_files, start=1):
        try:
            source_file = source_file.resolve()
            rel_path = source_file.relative_to(resolved_source.resolve())
            destination_file = resolved_destination / rel_path
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.add(destination_file.parent)
            if destination_file.exists() and not overwrite:
                skipped_files += 1
                skipped_paths.append(rel_path.as_posix())
                if on_log:
                    on_log(f"Skipping existing file: {rel_path.as_posix()}")
            else:
                if destination_file.exists():
                    overwritten_files += 1
                if not dry_run:
                    shutil.copy2(source_file, destination_file)
                copied_files += 1
                copied_paths.append(rel_path.as_posix())
                if on_log:
                    action = "DRYRUN COPY" if dry_run else "COPY"
                    on_log(f"{action} {rel_path.as_posix()}")
        except Exception:
            failed_files += 1
            failed_paths.append(str(source_file))
            if on_log:
                on_log(f"Failed to copy {source_file}")
        if on_progress:
            on_progress(index, total, f"{index} / {total} files")

    return LooseTreeCopyResult(
        source_root=resolved_source,
        destination_root=resolved_destination,
        total_files=total,
        copied_files=copied_files,
        skipped_files=skipped_files,
        overwritten_files=overwritten_files,
        created_dirs=len(created_dirs),
        failed_files=failed_files,
        copied_paths=copied_paths,
        skipped_paths=skipped_paths,
        failed_paths=failed_paths,
    )


def copy_mod_ready_loose_tree(
    source_root: Path,
    destination_root: Path,
    *,
    selected_paths: Optional[Sequence[Path | str]] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> LooseTreeCopyResult:
    return copy_loose_tree_preserving_paths(
        source_root,
        destination_root,
        selected_paths=selected_paths,
        overwrite=overwrite,
        dry_run=dry_run,
        on_progress=on_progress,
        on_log=on_log,
    )
