from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from cdmw.constants import (
    UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    UPSCALE_POST_CORRECTION_MATCH_LEVELS,
    UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
    UPSCALE_POST_CORRECTION_NONE,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
)
from cdmw.core.upscale_profiles import (
    TextureUpscaleDecision,
    suggest_texture_upscale_decision,
)

try:
    from PIL import Image, ImageStat
except Exception:  # pragma: no cover - optional runtime dependency
    Image = None  # type: ignore[assignment]
    ImageStat = None  # type: ignore[assignment]

_VISIBLE_TEXTURE_TYPES = frozenset({"color", "ui", "emissive", "impostor"})
_TECHNICAL_TEXTURE_TYPES = frozenset({"normal", "roughness", "height", "vector"})
_VISIBLE_SOURCE_MATCH_PROFILE_KEYS = frozenset({"color_default", "color_cutout_alpha", "ui_alpha"})
_PACKED_MASK_SUBTYPES = frozenset(
    {
        "orm",
        "rma",
        "mra",
        "arm",
        "packed_mask",
        "material_mask",
        "material_response",
        "ao",
        "metallic",
        "specular",
        "subsurface",
        "emissive_intensity",
    }
)
_SOURCE_MATCH_MODES = (
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
)
_POST_CORRECTION_MODES = (
    UPSCALE_POST_CORRECTION_NONE,
    UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
    UPSCALE_POST_CORRECTION_MATCH_LEVELS,
    UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    *_SOURCE_MATCH_MODES,
)


@dataclass(slots=True)
class SourceMatchPlan:
    mode: str
    correction_eligibility: str
    correction_action: str
    correction_reason: str
    scalar_only: bool = False
    allow_alpha_correction: bool = False


@dataclass(slots=True)
class SourceMatchResult:
    applied: bool
    mode: str
    detail: str
    correction_eligibility: str
    correction_action: str
    correction_reason: str


PostUpscaleCorrectionResult = SourceMatchResult


def is_post_upscale_correction_supported() -> bool:
    return Image is not None and ImageStat is not None


def post_upscale_correction_error_message() -> str:
    return "Pillow is required for post-upscale color correction."


def normalize_post_upscale_correction_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower() or UPSCALE_POST_CORRECTION_NONE
    if normalized not in _POST_CORRECTION_MODES:
        raise ValueError(f"Unsupported post-upscale correction mode: {mode}")
    return normalized


def describe_post_upscale_correction_mode(mode: str) -> str:
    normalized = normalize_post_upscale_correction_mode(mode)
    if normalized == UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA:
        return "Match Mean Luma"
    if normalized == UPSCALE_POST_CORRECTION_MATCH_LEVELS:
        return "Match Levels"
    if normalized == UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM:
        return "Match Histogram"
    if normalized == UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED:
        return "Source Match Balanced"
    if normalized == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED:
        return "Source Match Extended"
    if normalized == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL:
        return "Source Match Experimental"
    return "Off"


def is_source_match_correction_mode(mode: str) -> bool:
    return normalize_post_upscale_correction_mode(mode) in _SOURCE_MATCH_MODES


def should_apply_post_upscale_correction(texture_type: str) -> bool:
    return str(texture_type or "").strip().lower() in _VISIBLE_TEXTURE_TYPES


def build_source_match_plan_for_decision(
    mode: str,
    decision: TextureUpscaleDecision,
    *,
    direct_backend_supported: bool = True,
    planner_path_kind: str = "",
    planner_profile_key: str = "",
) -> SourceMatchPlan:
    normalized_mode = normalize_post_upscale_correction_mode(mode)
    texture_type = str(decision.texture_type or "").strip().lower()
    semantic_subtype = str(decision.semantic_subtype or "").strip().lower()
    alpha_mode = str(decision.alpha_mode or "").strip().lower()
    packed_channels = tuple(str(value or "").strip().lower() for value in decision.packed_channels)
    planner_path_kind = str(planner_path_kind or "").strip().lower()
    planner_profile_key = str(planner_profile_key or "").strip().lower()
    planner_visible_candidate = (
        planner_path_kind == "visible_color_png_path"
        and planner_profile_key in _VISIBLE_SOURCE_MATCH_PROFILE_KEYS
    )

    if normalized_mode == UPSCALE_POST_CORRECTION_NONE:
        return SourceMatchPlan(normalized_mode, "skip_disabled", "skip", "Correction is disabled.")

    if not direct_backend_supported:
        return SourceMatchPlan(
            normalized_mode,
            "skip_backend",
            "skip",
            "Only the direct NCNN backend currently supports post-upscale correction.",
        )

    if normalized_mode in {
        UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
        UPSCALE_POST_CORRECTION_MATCH_LEVELS,
        UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    }:
        if texture_type in _VISIBLE_TEXTURE_TYPES or planner_visible_candidate:
            return SourceMatchPlan(
                normalized_mode,
                "visible_rgb_safe",
                "apply_visible",
                "Legacy correction mode is limited to visible color-like textures or files the planner already routed through the visible-color path.",
                scalar_only=False,
                allow_alpha_correction=alpha_mode == "straight",
            )
        return SourceMatchPlan(
            normalized_mode,
            "skip_technical",
            "skip",
            "Legacy correction modes are limited to visible color, UI, emissive, and impostor textures.",
        )

    if decision.preserve_original_due_to_intermediate:
        return SourceMatchPlan(
            normalized_mode,
            "skip_preserve_original",
            "skip",
            "Automatic rules preserve the original DDS for this texture, so PNG source matching is skipped.",
        )

    if decision.precision_sensitive:
        return SourceMatchPlan(
            normalized_mode,
            "skip_technical",
            "skip",
            "Precision-sensitive source data should not receive source-match reconstruction.",
        )

    if texture_type in _TECHNICAL_TEXTURE_TYPES:
        return SourceMatchPlan(
            normalized_mode,
            "skip_technical",
            "skip",
            f"{texture_type} textures are treated as technical data and are not source-match corrected.",
        )

    if texture_type in _VISIBLE_TEXTURE_TYPES:
        if alpha_mode == "straight":
            return SourceMatchPlan(
                normalized_mode,
                "visible_rgb_safe",
                "apply_visible",
                "Visible texture will receive automatic RGB source matching with bounded alpha coverage correction.",
                scalar_only=False,
                allow_alpha_correction=True,
            )
        if alpha_mode in {"cutout", "premultiplied", "channel_data"}:
            return SourceMatchPlan(
                normalized_mode,
                "visible_rgb_alpha_limited",
                "apply_visible_limited",
                "Visible texture will receive RGB-only source matching while alpha is left untouched.",
                scalar_only=False,
                allow_alpha_correction=False,
            )
        return SourceMatchPlan(
            normalized_mode,
            "visible_rgb_safe",
            "apply_visible",
            "Visible texture will receive automatic RGB source matching.",
            scalar_only=False,
            allow_alpha_correction=False,
        )

    if texture_type == "mask":
        if semantic_subtype in _PACKED_MASK_SUBTYPES or packed_channels:
            return SourceMatchPlan(
                normalized_mode,
                "skip_technical",
                "skip",
                f"Packed or semantic mask data '{semantic_subtype or 'mask'}' is not source-match corrected.",
            )
        if semantic_subtype == "opacity_mask":
            if normalized_mode in {
                UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
                UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
            } and alpha_mode not in {"channel_data", "premultiplied"}:
                return SourceMatchPlan(
                    normalized_mode,
                    "scalar_safe",
                    "apply_scalar",
                    "Opacity mask is eligible for grayscale-only source matching in extended modes.",
                    scalar_only=True,
                    allow_alpha_correction=False,
                )
            return SourceMatchPlan(
                normalized_mode,
                "skip_technical",
                "skip",
                "Opacity masks only receive grayscale source matching in extended modes when alpha does not appear to carry technical data.",
            )
        if normalized_mode in {
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
        } and alpha_mode not in {"channel_data", "premultiplied"}:
            return SourceMatchPlan(
                normalized_mode,
                "scalar_safe",
                "apply_scalar",
                "Non-packed grayscale mask is eligible for scalar-only source matching in extended modes.",
                scalar_only=True,
                allow_alpha_correction=False,
            )
        return SourceMatchPlan(
            normalized_mode,
            "skip_technical",
            "skip",
            "Mask textures are only source-match corrected in extended modes when they appear to be safe grayscale data.",
        )

    if texture_type == "unknown":
        if planner_visible_candidate:
            if alpha_mode == "straight":
                return SourceMatchPlan(
                    normalized_mode,
                    "visible_rgb_safe",
                    "apply_visible",
                    "Semantic hint stayed unknown, but the planner still routed this file through a visible-color profile, so visible-texture source matching is allowed with bounded alpha coverage correction.",
                    scalar_only=False,
                    allow_alpha_correction=True,
                )
            if alpha_mode in {"cutout", "premultiplied", "channel_data"}:
                return SourceMatchPlan(
                    normalized_mode,
                    "visible_rgb_alpha_limited",
                    "apply_visible_limited",
                    "Semantic hint stayed unknown, but the planner still routed this file through a visible-color profile, so bounded RGB source matching is allowed while keeping alpha untouched.",
                    scalar_only=False,
                    allow_alpha_correction=False,
                )
            return SourceMatchPlan(
                normalized_mode,
                "visible_rgb_safe",
                "apply_visible",
                "Semantic hint stayed unknown, but the planner still routed this file through a visible-color profile, so visible-texture source matching is allowed.",
                scalar_only=False,
                allow_alpha_correction=False,
            )
        if normalized_mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL and alpha_mode != "channel_data":
            return SourceMatchPlan(
                normalized_mode,
                "experimental_only",
                "apply_visible_limited",
                "Unknown texture is only eligible in experimental mode after technical-risk checks pass.",
                scalar_only=False,
                allow_alpha_correction=alpha_mode == "straight",
            )
        return SourceMatchPlan(
            normalized_mode,
            "skip_technical",
            "skip",
            "Unknown textures are skipped unless experimental mode is selected.",
        )

    return SourceMatchPlan(
        normalized_mode,
        "skip_technical",
        "skip",
        f"{texture_type or 'unclassified'} textures are not source-match corrected.",
    )


def build_source_match_plan_for_path(
    *,
    relative_path: str,
    source_png_path: Path,
    mode: str,
    preset: str,
    enable_automatic_rules: bool,
    sidecar_texts: Sequence[str] = (),
    original_dds_path: Optional[Path] = None,
    direct_backend_supported: bool = True,
) -> Tuple[TextureUpscaleDecision, SourceMatchPlan]:
    original_texconv_format = ""
    has_alpha = image_has_alpha_channel(source_png_path)
    if original_dds_path is not None and original_dds_path.exists():
        try:
            # Local import avoids a hard module cycle with the pipeline module.
            from cdmw.core.pipeline import parse_dds

            dds_info = parse_dds(original_dds_path)
            original_texconv_format = dds_info.texconv_format
            has_alpha = has_alpha or dds_info.has_alpha
        except Exception:
            pass

    decision = suggest_texture_upscale_decision(
        relative_path,
        preset=preset,
        original_texconv_format=original_texconv_format,
        has_alpha=has_alpha,
        sidecar_texts=sidecar_texts,
        enable_automatic_rules=enable_automatic_rules,
    )
    return decision, build_source_match_plan_for_decision(
        mode,
        decision,
        direct_backend_supported=direct_backend_supported,
    )


def apply_post_upscale_color_correction(
    source_path: Path,
    output_path: Path,
    mode: str,
    *,
    correction_plan: Optional[SourceMatchPlan] = None,
) -> SourceMatchResult:
    normalized_mode = normalize_post_upscale_correction_mode(mode)
    plan = correction_plan or SourceMatchPlan(
        normalized_mode,
        "visible_rgb_safe",
        "apply_visible",
        "No explicit plan was provided, so visible-color correction was assumed.",
        scalar_only=False,
        allow_alpha_correction=False,
    )
    if normalized_mode == UPSCALE_POST_CORRECTION_NONE:
        return SourceMatchResult(False, normalized_mode, "disabled", plan.correction_eligibility, "skip", "disabled")
    if plan.correction_action == "skip":
        return SourceMatchResult(
            False,
            normalized_mode,
            f"{describe_post_upscale_correction_mode(normalized_mode)} skipped: {plan.correction_reason}",
            plan.correction_eligibility,
            plan.correction_action,
            plan.correction_reason,
        )
    if not is_post_upscale_correction_supported():
        raise RuntimeError(post_upscale_correction_error_message())

    source_rgb, source_alpha = _load_rgb_and_alpha(Path(source_path))
    output_rgb, output_alpha = _load_rgb_and_alpha(Path(output_path))

    if plan.scalar_only:
        corrected_rgb = _apply_scalar_source_match(source_rgb, output_rgb, normalized_mode)
    elif normalized_mode in {
        UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
        UPSCALE_POST_CORRECTION_MATCH_LEVELS,
        UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    }:
        corrected_rgb = _apply_legacy_visible_correction(source_rgb, output_rgb, normalized_mode)
    elif normalized_mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL:
        corrected_rgb = _apply_experimental_source_match(source_rgb, output_rgb)
    else:
        corrected_rgb = _apply_balanced_source_match(
            source_rgb,
            output_rgb,
            normalized_mode,
        )

    corrected_alpha = output_alpha
    if plan.allow_alpha_correction and source_alpha is not None and output_alpha is not None:
        corrected_alpha = _apply_alpha_source_match(source_alpha, output_alpha, normalized_mode)

    corrected_image = corrected_rgb
    if corrected_alpha is not None:
        corrected_image = corrected_rgb.copy()
        corrected_image.putalpha(corrected_alpha)
    corrected_image.save(Path(output_path), format="PNG")

    detail = _build_correction_detail(
        source_rgb,
        output_rgb,
        corrected_rgb,
        normalized_mode,
        plan,
    )
    return SourceMatchResult(
        True,
        normalized_mode,
        detail,
        plan.correction_eligibility,
        plan.correction_action,
        plan.correction_reason,
    )


def image_has_alpha_channel(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            ihdr_length = int.from_bytes(handle.read(4), "big")
            chunk_type = handle.read(4)
            if chunk_type != b"IHDR" or ihdr_length != 13:
                return False
            handle.read(8)
            handle.read(1)
            color_type = handle.read(1)
            return bool(color_type and color_type[0] in {4, 6})
    except OSError:
        return False


def _require_pillow() -> None:
    if not is_post_upscale_correction_supported():
        raise RuntimeError(post_upscale_correction_error_message())


def _load_rgb_and_alpha(path: Path) -> Tuple[Any, Optional[Any]]:
    _require_pillow()
    assert Image is not None
    if not path.exists() or not path.is_file():
        raise ValueError(f"PNG file does not exist: {path}")
    with Image.open(path) as image:
        alpha = image.getchannel("A").copy() if "A" in image.getbands() else None
        rgb = image.convert("RGB")
    return rgb, alpha


def _apply_legacy_visible_correction(source_rgb: Any, output_rgb: Any, mode: str) -> Any:
    source_y, _source_cb, _source_cr = _split_luma(source_rgb)
    output_y, output_cb, output_cr = _split_luma(output_rgb)
    if mode == UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA:
        corrected_y = _apply_mean_luma_match(source_y, output_y)
    elif mode == UPSCALE_POST_CORRECTION_MATCH_LEVELS:
        corrected_y = _apply_levels_match(source_y, output_y)
    else:
        corrected_y = _apply_histogram_match(source_y, output_y)
    return _merge_luma(corrected_y, output_cb, output_cr, None)


def _apply_balanced_source_match(source_rgb: Any, output_rgb: Any, mode: str) -> Any:
    assert Image is not None
    source_y, source_cb, source_cr = _split_luma(source_rgb)
    output_y, output_cb, output_cr = _split_luma(output_rgb)

    correction_strength = 0.68 if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED else 0.84
    channel_cap = 10.0 if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED else 18.0
    chroma_cap = 8.0 if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED else 14.0

    matched_y = _apply_levels_match(source_y, output_y)
    corrected_y = Image.blend(output_y, matched_y, correction_strength)

    corrected_cb = _blend_mean_shift(output_cb, source_cb, max_delta=chroma_cap, strength=0.45 if correction_strength < 0.8 else 0.70)
    corrected_cr = _blend_mean_shift(output_cr, source_cr, max_delta=chroma_cap, strength=0.45 if correction_strength < 0.8 else 0.70)
    corrected_rgb = _merge_luma(corrected_y, corrected_cb, corrected_cr, None)

    source_r, source_g, source_b = source_rgb.split()
    corrected_r, corrected_g, corrected_b = corrected_rgb.split()
    corrected_r = _blend_mean_shift(corrected_r, source_r, max_delta=channel_cap, strength=0.40 if correction_strength < 0.8 else 0.62)
    corrected_g = _blend_mean_shift(corrected_g, source_g, max_delta=channel_cap, strength=0.40 if correction_strength < 0.8 else 0.62)
    corrected_b = _blend_mean_shift(corrected_b, source_b, max_delta=channel_cap, strength=0.40 if correction_strength < 0.8 else 0.62)
    return Image.merge("RGB", (corrected_r, corrected_g, corrected_b))


def _apply_scalar_source_match(source_rgb: Any, output_rgb: Any, mode: str) -> Any:
    assert Image is not None
    source_l = source_rgb.convert("L")
    output_l = output_rgb.convert("L")
    if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL:
        corrected_l = _apply_histogram_match(source_l, output_l)
    else:
        corrected_l = _apply_levels_match(source_l, output_l)
        corrected_l = Image.blend(output_l, corrected_l, 0.88)
    return Image.merge("RGB", (corrected_l, corrected_l, corrected_l))


def _apply_experimental_source_match(source_rgb: Any, output_rgb: Any) -> Any:
    assert Image is not None
    source_channels = source_rgb.split()
    output_channels = output_rgb.split()
    corrected_channels = tuple(
        _apply_histogram_match(source_channel, output_channel)
        for source_channel, output_channel in zip(source_channels, output_channels)
    )
    return Image.merge("RGB", corrected_channels)


def _apply_alpha_source_match(source_alpha: Any, output_alpha: Any, mode: str) -> Any:
    assert Image is not None
    if mode == UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM:
        corrected = _apply_histogram_match(source_alpha, output_alpha)
        return Image.blend(output_alpha, corrected, 0.55)
    if mode == UPSCALE_POST_CORRECTION_MATCH_LEVELS:
        corrected = _apply_levels_match(source_alpha, output_alpha)
        return Image.blend(output_alpha, corrected, 0.55)
    if mode == UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA:
        corrected = _apply_mean_luma_match(source_alpha, output_alpha)
        return Image.blend(output_alpha, corrected, 0.45)
    if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED:
        corrected = _apply_levels_match(source_alpha, output_alpha)
        return Image.blend(output_alpha, corrected, 0.55)
    if mode == UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED:
        corrected = _apply_levels_match(source_alpha, output_alpha)
        return Image.blend(output_alpha, corrected, 0.72)
    corrected = _apply_histogram_match(source_alpha, output_alpha)
    return Image.blend(output_alpha, corrected, 0.88)


def _build_correction_detail(
    source_rgb: Any,
    output_rgb: Any,
    corrected_rgb: Any,
    mode: str,
    plan: SourceMatchPlan,
) -> str:
    source_y = source_rgb.convert("L")
    output_y = output_rgb.convert("L")
    corrected_y = corrected_rgb.convert("L")
    source_mean = _luma_mean(source_y)
    output_mean = _luma_mean(output_y)
    corrected_mean = _luma_mean(corrected_y)
    source_low, source_high = _histogram_bounds(source_y.histogram())
    output_low, output_high = _histogram_bounds(output_y.histogram())
    corrected_low, corrected_high = _histogram_bounds(corrected_y.histogram())
    return (
        f"{describe_post_upscale_correction_mode(mode)} [{plan.correction_eligibility}] "
        f"(mean {output_mean:.1f}->{corrected_mean:.1f} vs source {source_mean:.1f}; "
        f"range {output_low}-{output_high}->{corrected_low}-{corrected_high} vs source {source_low}-{source_high}; "
        f"{plan.correction_reason})"
    )


def _blend_mean_shift(channel: Any, source_channel: Any, *, max_delta: float, strength: float) -> Any:
    assert Image is not None
    shifted = _shift_channel_mean(channel, source_channel, max_delta=max_delta)
    return Image.blend(channel, shifted, max(0.0, min(1.0, float(strength))))


def _shift_channel_mean(channel: Any, source_channel: Any, *, max_delta: float) -> Any:
    delta = _channel_mean(source_channel) - _channel_mean(channel)
    delta = max(-abs(max_delta), min(abs(max_delta), delta))
    lut = [_clamp_byte(value + delta) for value in range(256)]
    return channel.point(lut)


def _split_luma(image: Any) -> Tuple[Any, Any, Any]:
    ycbcr = image.convert("YCbCr")
    return ycbcr.split()


def _merge_luma(luma_channel: Any, cb_channel: Any, cr_channel: Any, alpha_channel: Optional[Any]) -> Any:
    assert Image is not None
    corrected_rgb = Image.merge("YCbCr", (luma_channel, cb_channel, cr_channel)).convert("RGB")
    if alpha_channel is not None:
        corrected_rgb.putalpha(alpha_channel)
    return corrected_rgb


def _channel_mean(channel: Any) -> float:
    assert ImageStat is not None
    return float(ImageStat.Stat(channel).mean[0])


def _luma_mean(channel: Any) -> float:
    return _channel_mean(channel)


def _histogram_bounds(histogram: list[int], *, low_percentile: float = 0.01, high_percentile: float = 0.99) -> Tuple[int, int]:
    return _histogram_percentile(histogram, low_percentile), _histogram_percentile(histogram, high_percentile)


def _histogram_percentile(histogram: list[int], percentile: float) -> int:
    total = int(sum(histogram))
    if total <= 0:
        return 0
    target = max(0.0, min(1.0, float(percentile))) * float(total - 1)
    running = 0.0
    for index, count in enumerate(histogram):
        running += float(count)
        if running > target:
            return index
    return 255


def _apply_mean_luma_match(source_y: Any, output_y: Any) -> Any:
    delta = _luma_mean(source_y) - _luma_mean(output_y)
    lut = [_clamp_byte(value + delta) for value in range(256)]
    return output_y.point(lut)


def _apply_levels_match(source_y: Any, output_y: Any) -> Any:
    source_hist = source_y.histogram()
    output_hist = output_y.histogram()
    source_low, source_high = _histogram_bounds(source_hist)
    output_low, output_high = _histogram_bounds(output_hist)
    if source_high <= source_low or output_high <= output_low:
        return _apply_mean_luma_match(source_y, output_y)

    scale = float(source_high - source_low) / float(output_high - output_low)
    lut = [_clamp_byte(source_low + ((value - output_low) * scale)) for value in range(256)]
    return output_y.point(lut)


def _apply_histogram_match(source_y: Any, output_y: Any) -> Any:
    source_hist = source_y.histogram()
    output_hist = output_y.histogram()
    source_cdf = _build_cdf(source_hist)
    output_cdf = _build_cdf(output_hist)
    lut: list[int] = []
    source_index = 0
    for value in range(256):
        target = output_cdf[value]
        while source_index < 255 and source_cdf[source_index] < target:
            source_index += 1
        lut.append(source_index)
    return output_y.point(lut)


def _build_cdf(histogram: list[int]) -> list[float]:
    total = float(sum(histogram))
    if total <= 0.0:
        return [0.0] * 256
    cdf: list[float] = []
    running = 0.0
    for count in histogram:
        running += float(count)
        cdf.append(running / total)
    return cdf


def _clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(float(value)))))
