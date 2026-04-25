from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast

try:
    from PIL import Image as PilImage
except Exception:  # pragma: no cover - optional preview helper
    PilImage = None  # type: ignore[assignment]

from cdmw.constants import *
from cdmw.models import *
from cdmw.core.common import *
from cdmw.core.chainner import *
from cdmw.core.mod_package import (
    ModPackageExportOptions,
    mod_package_export_options_for_manager,
    mod_package_profile_uses_manager_metadata,
    resolve_mod_package_root,
    write_mod_package_manifest,
)
from cdmw.core.realesrgan_ncnn import *
from cdmw.core.upscale_postprocess import (
    build_source_match_plan_for_decision,
    describe_post_upscale_correction_mode,
)
from cdmw.core.upscale_profiles import (
    classify_texture_type,
    copy_mod_ready_loose_tree,
    derive_texture_group_key,
    is_png_intermediate_high_risk,
    is_technical_texture_type,
    normalize_texture_reference_for_sidecar_lookup,
    parse_texture_sidecar_bindings,
    should_upscale_texture,
    suggest_texture_upscale_decision,
    TexturePreviewSample,
    TextureUpscaleDecision,
)

_DDS_ALPHA_CAPABLE_FORMATS = {
    "R8G8B8A8_UNORM",
    "R8G8B8A8_UNORM_SRGB",
    "B8G8R8A8_UNORM",
    "B8G8R8A8_UNORM_SRGB",
    "BC1_UNORM",
    "BC1_UNORM_SRGB",
    "BC2_UNORM",
    "BC2_UNORM_SRGB",
    "BC3_UNORM",
    "BC3_UNORM_SRGB",
    "BC7_UNORM",
    "BC7_UNORM_SRGB",
    "R16G16B16A16_FLOAT",
    "R16G16B16A16_SNORM",
    "R32G32B32A32_FLOAT",
}
_LOOSE_SEMANTIC_SIDECAR_EXTENSIONS = {
    ".xml",
    ".pami",
    ".material",
    ".shader",
    ".json",
    ".lua",
    ".txt",
    ".ini",
    ".cfg",
    ".yaml",
    ".yml",
}
_LOOSE_SIDECAR_TEXT_LIMIT = 196_608
_PREVIEW_CACHE_LOCKS: Dict[str, threading.Lock] = {}
_PREVIEW_CACHE_LOCKS_GUARD = threading.Lock()
_VISIBLE_COLOR_TEXTURE_TYPES = frozenset({"color", "ui", "emissive", "impostor"})
_COMPARE_DISPLAY_PREVIEW_MAX_DIMENSION = 1536
_VALID_RULE_COLORSPACE_OVERRIDES = frozenset({"srgb", "linear", "match_source"})
_VALID_RULE_ALPHA_POLICIES = frozenset({"none", "straight", "cutout_coverage", "channel_data", "premultiplied"})
_VALID_RULE_INTERMEDIATE_OVERRIDES = frozenset(
    {"visible_color_png_path", "technical_preserve_path", "technical_high_precision_path"}
)
_VALID_RULE_MATCH_MODES = frozenset({"glob", "exact"})
_VALID_WORKFLOW_PROFILE_ACTIONS = frozenset(
    {"", "upscale_then_rebuild", "rebuild_from_png", "preserve_original", "skip"}
)
_SCALAR_HIGH_PRECISION_MASK_SUBTYPES = frozenset(
    {
        "mask",
        "ao",
        "grayscale_data",
        "opacity_mask",
        "detail_support",
        "metallic",
        "specular",
        "subsurface",
        "emissive_intensity",
    }
)

_DEFAULT_SEMANTIC_SUBTYPES: Dict[str, str] = {
    "color": "albedo",
    "ui": "ui",
    "emissive": "emissive",
    "impostor": "impostor",
    "normal": "normal",
    "world_normal": "normal",
    "height": "height",
    "vector": "vector",
    "roughness": "roughness",
    "gloss_or_smoothness": "roughness",
    "mask": "mask",
    "unknown": "unknown",
}

_SEMANTIC_OVERRIDE_TEXTURE_TYPES: Dict[str, str] = {
    "albedo": "color",
    "albedo_variant": "color",
    "ui": "ui",
    "emissive": "emissive",
    "impostor": "impostor",
    "normal": "normal",
    "height": "height",
    "displacement": "height",
    "bump": "height",
    "parallax_height": "height",
    "vector": "vector",
    "direction_vector": "vector",
    "effect_vector": "vector",
    "pivot_position": "vector",
    "flow_vector": "vector",
    "position_vector": "vector",
    "roughness": "roughness",
    "mask": "mask",
    "orm": "mask",
    "rma": "mask",
    "mra": "mask",
    "arm": "mask",
    "packed_mask": "mask",
    "opacity_mask": "mask",
    "material_mask": "mask",
    "material_response": "mask",
    "ao": "mask",
    "metallic": "mask",
    "specular": "mask",
    "detail_support": "mask",
    "subsurface": "mask",
    "emissive_intensity": "mask",
    "grayscale_data": "mask",
    "unknown": "unknown",
}

_PROFILE_TABLE: Dict[str, TextureProcessingProfile] = {
    "color_default": TextureProcessingProfile(
        key="color_default",
        label="Color Default",
        allowed_intermediate_kinds=("visible_color_png_path",),
        preferred_texconv_format="BC7_UNORM_SRGB",
        colorspace_policy="srgb",
        alpha_policy="straight",
        mip_policy_hint="standard_color",
    ),
    "color_cutout_alpha": TextureProcessingProfile(
        key="color_cutout_alpha",
        label="Color Cutout Alpha",
        allowed_intermediate_kinds=("visible_color_png_path",),
        preferred_texconv_format="BC7_UNORM_SRGB",
        colorspace_policy="srgb",
        alpha_policy="cutout_coverage",
        mip_policy_hint="keep_coverage",
    ),
    "ui_alpha": TextureProcessingProfile(
        key="ui_alpha",
        label="UI Alpha",
        allowed_intermediate_kinds=("visible_color_png_path",),
        preferred_texconv_format="BC7_UNORM_SRGB",
        colorspace_policy="srgb",
        alpha_policy="straight",
        mip_policy_hint="ui_alpha_safe",
    ),
    "normal_bc5": TextureProcessingProfile(
        key="normal_bc5",
        label="Normal BC5",
        allowed_intermediate_kinds=("technical_preserve_path",),
        preferred_texconv_format="BC5_UNORM",
        colorspace_policy="linear",
        alpha_policy="none",
        mip_policy_hint="normal_linear",
        preserve_only=True,
    ),
    "scalar_bc4": TextureProcessingProfile(
        key="scalar_bc4",
        label="Scalar BC4",
        allowed_intermediate_kinds=("technical_preserve_path",),
        preferred_texconv_format="BC4_UNORM",
        colorspace_policy="linear",
        alpha_policy="none",
        mip_policy_hint="scalar_linear",
        preserve_only=True,
    ),
    "scalar_high_precision_bc4": TextureProcessingProfile(
        key="scalar_high_precision_bc4",
        label="Scalar High Precision BC4",
        allowed_intermediate_kinds=("technical_high_precision_path",),
        preferred_texconv_format="BC4_UNORM",
        colorspace_policy="linear",
        alpha_policy="none",
        mip_policy_hint="scalar_high_precision",
        preserve_only=False,
    ),
    "packed_mask_preserve_layout": TextureProcessingProfile(
        key="packed_mask_preserve_layout",
        label="Packed Mask Preserve Layout",
        allowed_intermediate_kinds=("technical_preserve_path",),
        preferred_texconv_format="MATCH_ORIGINAL",
        colorspace_policy="linear",
        alpha_policy="channel_data",
        mip_policy_hint="preserve_channels",
        preserve_only=True,
    ),
    "premultiplied_alpha_review_required": TextureProcessingProfile(
        key="premultiplied_alpha_review_required",
        label="Premultiplied Alpha Review Required",
        allowed_intermediate_kinds=("technical_preserve_path",),
        preferred_texconv_format="MATCH_ORIGINAL",
        colorspace_policy="match_source",
        alpha_policy="premultiplied",
        mip_policy_hint="review_required",
        preserve_only=True,
    ),
    "float_or_vector_preserve_only": TextureProcessingProfile(
        key="float_or_vector_preserve_only",
        label="Float Or Vector Preserve Only",
        allowed_intermediate_kinds=("technical_preserve_path",),
        preferred_texconv_format="MATCH_ORIGINAL",
        colorspace_policy="match_source",
        alpha_policy="none",
        mip_policy_hint="preserve_precision",
        preserve_only=True,
    ),
}


def get_texture_processing_profile_keys() -> Tuple[str, ...]:
    return tuple(sorted(_PROFILE_TABLE))


def build_default_texture_workflow_profiles() -> Tuple[TextureWorkflowProfile, ...]:
    return (
        TextureWorkflowProfile(
            profile_id="starter_color_albedo",
            label="Starter Color / Albedo",
            action_mode="upscale_then_rebuild",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
        ),
        TextureWorkflowProfile(
            profile_id="starter_normal_map",
            label="Starter Normal",
            action_mode="preserve_original",
            format_value="BC5_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_height_displacement",
            label="Starter Height / Displacement",
            action_mode="preserve_original",
            format_value=DDS_FORMAT_MODE_MATCH_ORIGINAL,
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_specular",
            label="Starter Specular",
            action_mode="preserve_original",
            format_value=DDS_FORMAT_MODE_MATCH_ORIGINAL,
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
    )


def build_default_texture_workflow_rules() -> Tuple[TextureRule, ...]:
    return (
        TextureRule(
            pattern="*.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_color_albedo",
            colorspace_value="match_source",
            alpha_policy_value="straight",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *.dds",
        ),
        TextureRule(
            pattern="*_n.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_normal_map",
            semantic_value="normal:normal",
            profile_value="normal_bc5",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="technical_preserve_path",
            source_line="default starter rule: *_n.dds",
        ),
        TextureRule(
            pattern="*_d.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_height_displacement",
            semantic_value="height:displacement",
            profile_value="scalar_high_precision_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="technical_high_precision_path",
            source_line="default starter rule: *_d.dds",
        ),
        TextureRule(
            pattern="*_disp.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_height_displacement",
            semantic_value="height:displacement",
            profile_value="scalar_high_precision_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="technical_high_precision_path",
            source_line="default starter rule: *_disp.dds",
        ),
        TextureRule(
            pattern="*_sp.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_specular",
            semantic_value="mask:specular",
            profile_value="scalar_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="technical_preserve_path",
            source_line="default starter rule: *_sp.dds",
        ),
    )


def _build_legacy_default_texture_workflow_profiles() -> Tuple[TextureWorkflowProfile, ...]:
    return (
        TextureWorkflowProfile(
            profile_id="starter_color_albedo",
            label="Starter Color / Albedo",
            action_mode="upscale_then_rebuild",
            format_value="BC7_UNORM_SRGB",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=4,
            post_correction_mode=UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
        ),
        TextureWorkflowProfile(
            profile_id="starter_normal_map",
            label="Starter Normal",
            action_mode="upscale_then_rebuild",
            format_value="BC5_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_height_displacement",
            label="Starter Height / Displacement",
            action_mode="upscale_then_rebuild",
            format_value="BC4_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_specular",
            label="Starter Specular",
            action_mode="upscale_then_rebuild",
            format_value="BC4_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
    )


def _build_pre_conservative_specular_texture_workflow_profiles() -> Tuple[TextureWorkflowProfile, ...]:
    profiles = list(build_default_texture_workflow_profiles())
    for index, profile in enumerate(profiles):
        if profile.profile_id == "starter_specular":
            profiles[index] = replace(profile, format_value="BC4_UNORM")
            break
    return tuple(profiles)


def _build_pre_inherit_scale_texture_workflow_profiles() -> Tuple[TextureWorkflowProfile, ...]:
    return (
        TextureWorkflowProfile(
            profile_id="starter_color_albedo",
            label="Starter Color / Albedo",
            action_mode="upscale_then_rebuild",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=4,
            post_correction_mode=UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
        ),
        TextureWorkflowProfile(
            profile_id="starter_normal_map",
            label="Starter Normal",
            action_mode="upscale_then_rebuild",
            format_value="BC5_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_height_displacement",
            label="Starter Height / Displacement",
            action_mode="upscale_then_rebuild",
            format_value="BC4_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_specular",
            label="Starter Specular",
            action_mode="upscale_then_rebuild",
            format_value=DDS_FORMAT_MODE_MATCH_ORIGINAL,
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            ncnn_scale=2,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
    )


def _build_pre_conservative_technical_texture_workflow_profiles() -> Tuple[TextureWorkflowProfile, ...]:
    return (
        TextureWorkflowProfile(
            profile_id="starter_color_albedo",
            label="Starter Color / Albedo",
            action_mode="upscale_then_rebuild",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
        ),
        TextureWorkflowProfile(
            profile_id="starter_normal_map",
            label="Starter Normal",
            action_mode="upscale_then_rebuild",
            format_value="BC5_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_height_displacement",
            label="Starter Height / Displacement",
            action_mode="upscale_then_rebuild",
            format_value="BC4_UNORM",
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
        TextureWorkflowProfile(
            profile_id="starter_specular",
            label="Starter Specular",
            action_mode="upscale_then_rebuild",
            format_value=DDS_FORMAT_MODE_MATCH_ORIGINAL,
            size_value=DDS_SIZE_MODE_PNG,
            mip_value=DDS_MIP_MODE_FULL_CHAIN,
            post_correction_mode=UPSCALE_POST_CORRECTION_NONE,
        ),
    )


def _build_legacy_default_texture_workflow_rules() -> Tuple[TextureRule, ...]:
    return (
        TextureRule(
            pattern="*.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_color_albedo",
            profile_value="color_default",
            colorspace_value="srgb",
            alpha_policy_value="straight",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *.dds",
        ),
        TextureRule(
            pattern="*_n.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_normal_map",
            semantic_value="normal:normal",
            profile_value="normal_bc5",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_n.dds",
        ),
        TextureRule(
            pattern="*_d.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_height_displacement",
            semantic_value="height:displacement",
            profile_value="scalar_high_precision_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_d.dds",
        ),
        TextureRule(
            pattern="*_sp.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_specular",
            semantic_value="mask:specular",
            profile_value="scalar_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_sp.dds",
        ),
    )


def _build_pre_conservative_technical_texture_workflow_rules() -> Tuple[TextureRule, ...]:
    return (
        TextureRule(
            pattern="*.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_color_albedo",
            colorspace_value="match_source",
            alpha_policy_value="straight",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *.dds",
        ),
        TextureRule(
            pattern="*_n.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_normal_map",
            semantic_value="normal:normal",
            profile_value="normal_bc5",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_n.dds",
        ),
        TextureRule(
            pattern="*_d.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_height_displacement",
            semantic_value="height:displacement",
            profile_value="scalar_high_precision_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_d.dds",
        ),
        TextureRule(
            pattern="*_disp.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_height_displacement",
            semantic_value="height:displacement",
            profile_value="scalar_high_precision_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_disp.dds",
        ),
        TextureRule(
            pattern="*_sp.dds",
            enabled=True,
            match_mode="glob",
            workflow_profile_id="starter_specular",
            semantic_value="mask:specular",
            profile_value="scalar_bc4",
            colorspace_value="linear",
            alpha_policy_value="none",
            intermediate_value="visible_color_png_path",
            source_line="default starter rule: *_sp.dds",
        ),
    )


def upgrade_default_texture_workflow_state(
    workflow_profiles: Sequence[TextureWorkflowProfile],
    texture_rules: Sequence[TextureRule],
) -> Tuple[Tuple[TextureWorkflowProfile, ...], Tuple[TextureRule, ...]]:
    current_profiles = {profile.profile_id: profile for profile in build_default_texture_workflow_profiles()}
    legacy_profiles = {profile.profile_id: profile for profile in _build_legacy_default_texture_workflow_profiles()}
    pre_conservative_specular_profiles = {
        profile.profile_id: profile for profile in _build_pre_conservative_specular_texture_workflow_profiles()
    }
    pre_inherit_scale_profiles = {
        profile.profile_id: profile for profile in _build_pre_inherit_scale_texture_workflow_profiles()
    }
    pre_conservative_technical_profiles = {
        profile.profile_id: profile for profile in _build_pre_conservative_technical_texture_workflow_profiles()
    }
    upgraded_profiles: List[TextureWorkflowProfile] = []
    profiles_changed = False
    for profile in workflow_profiles:
        replacement = current_profiles.get(profile.profile_id)
        legacy = legacy_profiles.get(profile.profile_id)
        pre_conservative_specular = pre_conservative_specular_profiles.get(profile.profile_id)
        pre_inherit_scale = pre_inherit_scale_profiles.get(profile.profile_id)
        pre_conservative_technical = pre_conservative_technical_profiles.get(profile.profile_id)
        if replacement is not None and (
            (legacy is not None and profile == legacy)
            or (pre_conservative_specular is not None and profile == pre_conservative_specular)
            or (pre_inherit_scale is not None and profile == pre_inherit_scale)
            or (pre_conservative_technical is not None and profile == pre_conservative_technical)
        ):
            upgraded_profiles.append(replacement)
            profiles_changed = True
        else:
            upgraded_profiles.append(profile)

    current_rules = {rule.pattern: rule for rule in build_default_texture_workflow_rules()}
    legacy_rules = {rule.pattern: rule for rule in _build_legacy_default_texture_workflow_rules()}
    pre_conservative_technical_rules = {
        rule.pattern: rule for rule in _build_pre_conservative_technical_texture_workflow_rules()
    }
    upgraded_rules: List[TextureRule] = []
    rules_changed = False
    for rule in texture_rules:
        replacement = current_rules.get(rule.pattern)
        legacy = legacy_rules.get(rule.pattern)
        pre_conservative_technical = pre_conservative_technical_rules.get(rule.pattern)
        if replacement is not None and (
            (legacy is not None and rule == legacy)
            or (pre_conservative_technical is not None and rule == pre_conservative_technical)
        ):
            upgraded_rules.append(replacement)
            rules_changed = True
        else:
            upgraded_rules.append(rule)

    upgraded_rule_by_pattern = {str(rule.pattern or "").strip().lower(): rule for rule in upgraded_rules}
    if "*_disp.dds" not in upgraded_rule_by_pattern:
        starter_height_rule_variants = tuple(
            rule
            for rule in (
                current_rules.get("*_d.dds"),
                legacy_rules.get("*_d.dds"),
            )
            if rule is not None
        )
        existing_height_rule = upgraded_rule_by_pattern.get("*_d.dds")
        if existing_height_rule is not None and any(existing_height_rule == variant for variant in starter_height_rule_variants):
            disp_rule = current_rules.get("*_disp.dds")
            if disp_rule is not None:
                upgraded_rules.append(disp_rule)
                rules_changed = True

    if not profiles_changed and not rules_changed:
        return tuple(workflow_profiles), tuple(texture_rules)
    return tuple(upgraded_profiles), tuple(upgraded_rules)


def _is_blank_workflow_profile_placeholder(profile: TextureWorkflowProfile) -> bool:
    if not re.fullmatch(r"Profile(?:\s+\d+)?", str(profile.label or "").strip(), flags=re.IGNORECASE):
        return False
    return not any(
        [
            str(profile.action_mode or "").strip(),
            str(profile.format_value or "").strip(),
            str(profile.size_value or "").strip(),
            str(profile.mip_value or "").strip(),
            str(profile.ncnn_model_name or "").strip(),
            profile.ncnn_scale is not None,
            profile.ncnn_tile_size is not None,
            str(profile.ncnn_extra_args or "").strip(),
            str(profile.post_correction_mode or "").strip(),
        ]
    )


def _is_blank_workflow_rule_placeholder(rule: TextureRule) -> bool:
    return (
        str(rule.pattern or "").strip().lower() == "*.dds"
        and bool(rule.enabled)
        and str(rule.match_mode or "glob").strip().lower() == "glob"
        and str(rule.action or "process").strip().lower() == "process"
        and not any(
            [
                str(rule.format_value or "").strip(),
                str(rule.size_value or "").strip(),
                str(rule.mip_value or "").strip(),
                str(rule.semantic_value or "").strip(),
                str(rule.profile_value or "").strip(),
                str(rule.colorspace_value or "").strip(),
                str(rule.alpha_policy_value or "").strip(),
                str(rule.intermediate_value or "").strip(),
                str(rule.workflow_profile_id or "").strip(),
            ]
        )
    )


def should_seed_default_texture_workflow_state(
    workflow_profiles: Sequence[TextureWorkflowProfile],
    texture_rules: Sequence[TextureRule],
) -> bool:
    if not workflow_profiles and not texture_rules:
        return True
    if len(texture_rules) == 1 and _is_blank_workflow_rule_placeholder(texture_rules[0]):
        if not workflow_profiles:
            return True
        if len(workflow_profiles) == 1 and _is_blank_workflow_profile_placeholder(workflow_profiles[0]):
            return True
    return False


def _get_preview_cache_lock(cache_key: str) -> threading.Lock:
    with _PREVIEW_CACHE_LOCKS_GUARD:
        lock = _PREVIEW_CACHE_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _PREVIEW_CACHE_LOCKS[cache_key] = lock
        return lock

def _dds_colorspace_intent_from_format(texconv_format: str) -> str:
    normalized = str(texconv_format or "").strip().upper()
    if normalized.endswith("_SRGB"):
        return "srgb"
    if normalized:
        return "linear"
    return "unknown"


def _legacy_luminance_texconv_format(
    rgb_bit_count: int,
    r_mask: int,
    g_mask: int,
    b_mask: int,
    a_mask: int,
) -> Optional[str]:
    mask_tuple = (r_mask, g_mask, b_mask, a_mask)
    if rgb_bit_count == 8 and mask_tuple == (
        0x000000FF,
        0x00000000,
        0x00000000,
        0x00000000,
    ):
        return "R8_UNORM"
    if rgb_bit_count == 16 and mask_tuple == (
        0x0000FFFF,
        0x00000000,
        0x00000000,
        0x00000000,
    ):
        return "R16_UNORM"
    if rgb_bit_count == 16 and mask_tuple in {
        (
            0x000000FF,
            0x00000000,
            0x00000000,
            0x0000FF00,
        ),
        (
            0x0000FF00,
            0x00000000,
            0x00000000,
            0x000000FF,
        ),
    }:
        return "R8G8_UNORM"
    return None


def _legacy_alpha_texconv_format(
    rgb_bit_count: int,
    r_mask: int,
    g_mask: int,
    b_mask: int,
    a_mask: int,
) -> Optional[str]:
    mask_tuple = (r_mask, g_mask, b_mask, a_mask)
    if rgb_bit_count == 8 and mask_tuple in {
        (0x00000000, 0x00000000, 0x00000000, 0x000000FF),
        (0x000000FF, 0x00000000, 0x00000000, 0x00000000),
        (0x00000000, 0x00000000, 0x00000000, 0x00000000),
    }:
        return "A8_UNORM"
    if rgb_bit_count == 16 and mask_tuple in {
        (0x00000000, 0x00000000, 0x00000000, 0x0000FFFF),
        (0x0000FFFF, 0x00000000, 0x00000000, 0x00000000),
    }:
        return "R16_UNORM"
    return None

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

    has_alpha = bool(pf_flags & (DDPF_ALPHAPIXELS | DDPF_ALPHA))

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
                numeric_fourcc = read_u32_le(fourcc, 0)
                texconv_format = LEGACY_NUMERIC_FOURCC_TO_TEXCONV.get(numeric_fourcc)
            if not texconv_format:
                pretty_fourcc = fourcc.decode("ascii", errors="replace")
                raise ValueError(
                    f"Unsupported legacy FOURCC format: {pretty_fourcc!r} (numeric={read_u32_le(fourcc, 0)})"
                )
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
    elif pf_flags & DDPF_LUMINANCE:
        texconv_format = _legacy_luminance_texconv_format(rgb_bit_count, r_mask, g_mask, b_mask, a_mask)
        if not texconv_format:
            raise ValueError(
                "Unsupported luminance mask combination: "
                f"bits={rgb_bit_count} R={r_mask:#010x} G={g_mask:#010x} B={b_mask:#010x} A={a_mask:#010x}"
            )
    elif pf_flags & DDPF_ALPHA:
        texconv_format = _legacy_alpha_texconv_format(rgb_bit_count, r_mask, g_mask, b_mask, a_mask)
        if not texconv_format:
            raise ValueError(
                "Unsupported alpha-only mask combination: "
                f"bits={rgb_bit_count} R={r_mask:#010x} G={g_mask:#010x} B={b_mask:#010x} A={a_mask:#010x}"
            )
    else:
        raise ValueError(f"Unsupported DDS pixel format flags: {pf_flags:#x}")

    if texconv_format in _DDS_ALPHA_CAPABLE_FORMATS:
        has_alpha = True

    return DdsInfo(
        width=width,
        height=height,
        mip_count=max(1, mip_count),
        texconv_format=texconv_format,
        source_path=dds_path,
        has_alpha=has_alpha,
        colorspace_intent=_dds_colorspace_intent_from_format(texconv_format),
        precision_sensitive=("FLOAT" in texconv_format.upper() or "SNORM" in texconv_format.upper()),
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


def read_png_header_info(png_path: Path) -> Tuple[int, int, int, int]:
    with png_path.open("rb") as handle:
        signature = handle.read(8)
        if signature != PNG_MAGIC:
            raise ValueError("Not a PNG file or PNG signature is invalid.")
        ihdr_len = struct.unpack(">I", handle.read(4))[0]
        chunk_type = handle.read(4)
        if chunk_type != b"IHDR" or ihdr_len != 13:
            raise ValueError("PNG IHDR chunk is missing or invalid.")
        width, height = struct.unpack(">II", handle.read(8))
        bit_depth = handle.read(1)
        color_type = handle.read(1)
        if len(bit_depth) != 1 or len(color_type) != 1:
            raise ValueError("PNG IHDR bit depth or color type is missing.")
        return width, height, bit_depth[0], color_type[0]


def describe_png_color_type(color_type: int) -> str:
    return {
        0: "grayscale",
        2: "rgb",
        3: "indexed",
        4: "grayscale_alpha",
        6: "rgba",
    }.get(int(color_type), f"unknown({color_type})")


def png_has_alpha_channel(png_path: Path) -> bool:
    _width, _height, _bit_depth, color_type = read_png_header_info(png_path)
    return color_type in {4, 6}


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


def require_existing_dir(path: Optional[Path], label: str) -> Path:
    if path is None:
        raise ValueError(f"{label} is not set.")
    return ensure_existing_dir(path, label)


def require_existing_file(path: Optional[Path], label: str) -> Path:
    if path is None:
        raise ValueError(f"{label} is not set.")
    return ensure_existing_file(path, label)


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


def find_png_matches_across_roots(
    png_roots: Sequence[Optional[Path]],
    stop_event: Optional[threading.Event] = None,
) -> Tuple[Dict[str, Path], Dict[str, List[Path]], int]:
    relative_index: Dict[str, Path] = {}
    basename_index: Dict[str, List[Path]] = defaultdict(list)
    total_count = 0
    seen_roots: set[str] = set()

    for root in png_roots:
        if root is None:
            continue
        try:
            normalized_root_key = str(root.resolve())
        except OSError:
            normalized_root_key = str(root)
        if normalized_root_key in seen_roots:
            continue
        seen_roots.add(normalized_root_key)
        root_relative_index, root_basename_index, root_count = find_png_matches(root, stop_event=stop_event)
        relative_index.update(root_relative_index)
        for basename, paths in root_basename_index.items():
            basename_index[basename].extend(paths)
        total_count += root_count

    return relative_index, basename_index, total_count


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
    color_args: Optional[Sequence[str]] = None,
    extra_args: Optional[Sequence[str]] = None,
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

    if color_args:
        cmd.extend(str(arg) for arg in color_args if str(arg).strip())
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args if str(arg).strip())

    cmd.append(str(png_path))
    return cmd


def resolve_default_mod_ready_export_root(output_root: Path) -> Path:
    return output_root.parent / f"{output_root.name}_{MOD_READY_EXPORT_DIRNAME}"


def build_mod_package_export_options_from_config(config: AppConfig) -> ModPackageExportOptions:
    profile = str(getattr(config, "mod_ready_manager_profile", "universal") or "universal").strip() or "universal"
    defaults = mod_package_export_options_for_manager(profile)
    structure = str(getattr(config, "mod_ready_package_structure", "") or "").strip().lower()
    if structure not in {"game_relative", "files_wrapper"}:
        structure = defaults.structure
    conflict_mode = str(getattr(config, "mod_ready_conflict_mode", "") or "").strip().lower()
    if conflict_mode not in {"", "override"}:
        conflict_mode = ""
    if not mod_package_profile_uses_manager_metadata(profile):
        conflict_mode = ""
        target_language = ""
    else:
        target_language = str(getattr(config, "mod_ready_target_language", "") or "").strip()
    return ModPackageExportOptions(
        manager_targets=defaults.manager_targets,
        structure=structure,
        create_manifest_json=bool(getattr(config, "mod_ready_create_manifest_json", defaults.create_manifest_json)),
        create_mod_json=bool(getattr(config, "mod_ready_create_mod_json", defaults.create_mod_json)),
        create_modinfo_json=bool(getattr(config, "mod_ready_create_modinfo_json", defaults.create_modinfo_json)),
        create_info_json=bool(getattr(config, "mod_ready_create_info_json", defaults.create_info_json)),
        create_no_encrypt_file=bool(getattr(config, "mod_ready_create_no_encrypt_file", defaults.create_no_encrypt_file)),
        create_zip=bool(getattr(config, "mod_ready_create_zip", defaults.create_zip)),
        conflict_mode=conflict_mode,
        target_language=target_language,
        files_dir=defaults.files_dir,
    )


def common_workspace_root_from_config(config: AppConfig) -> Optional[Path]:
    candidates: List[Path] = []
    for raw in (
        config.original_dds_root,
        config.png_root,
        getattr(config, "texture_editor_png_root", ""),
        config.output_root,
        config.dds_staging_root,
        config.archive_extract_root,
        config.mod_ready_export_root,
    ):
        text = str(raw).strip()
        if not text:
            continue
        candidates.append(Path(text).expanduser())

    if len(candidates) < 2:
        return None

    try:
        common = Path(os.path.commonpath([str(path) for path in candidates]))
    except ValueError:
        return None

    if common.name.lower() in {
        "input_dds",
        "png_upscaled",
        "png_texture_editor",
        "dds_final",
        "png_staged_input",
        "archive_extract",
        MOD_READY_EXPORT_DIRNAME.lower(),
    }:
        return common.parent
    return common


def suggested_workspace_paths(base_dir: Path) -> Dict[str, Path]:
    base = base_dir.expanduser().resolve()
    tools_root = base / "tools"
    chainner_dir = tools_root / "chaiNNer"
    ncnn_dir = tools_root / "realesrgan_ncnn"
    output_root = base / "dds_final"
    return {
        "original_dds_root": base / "input_dds",
        "png_root": base / "png_upscaled",
        "texture_editor_png_root": base / "png_texture_editor",
        "dds_staging_root": base / "png_staged_input",
        "output_root": output_root,
        "archive_extract_root": base / "archive_extract",
        "tools_root": tools_root,
        "texconv_path": tools_root / "texconv.exe",
        "chainner_dir": chainner_dir,
        "chainner_exe_path": chainner_dir / "chaiNNer.exe",
        "ncnn_dir": ncnn_dir,
        "ncnn_exe_path": ncnn_dir / "realesrgan-ncnn-vulkan.exe",
        "ncnn_model_dir": ncnn_dir / "models",
        "mod_ready_export_root": resolve_default_mod_ready_export_root(output_root),
        "csv_log_path": base / "build_log.csv",
    }


def create_workspace_structure(base_dir: Path) -> Dict[str, Path]:
    paths = suggested_workspace_paths(base_dir)
    for key in (
        "original_dds_root",
        "png_root",
        "texture_editor_png_root",
        "dds_staging_root",
        "output_root",
        "archive_extract_root",
        "tools_root",
        "chainner_dir",
        "ncnn_dir",
        "ncnn_model_dir",
        "mod_ready_export_root",
    ):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def create_missing_directories_for_config(config: AppConfig) -> List[Path]:
    created: List[Path] = []

    def ensure_dir(path: Path) -> None:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)

    for raw in (
        config.original_dds_root,
        config.png_root,
        getattr(config, "texture_editor_png_root", ""),
        config.output_root,
        config.dds_staging_root,
        config.archive_extract_root,
        config.mod_ready_export_root,
        config.ncnn_model_dir,
    ):
        text = str(raw).strip()
        if text:
            ensure_dir(Path(text).expanduser().resolve())

    if config.csv_log_enabled and config.csv_log_path.strip():
        ensure_dir(Path(config.csv_log_path).expanduser().resolve().parent)

    for raw in (
        config.texconv_path,
        config.chainner_exe_path,
        config.chainner_chain_path,
        config.ncnn_exe_path,
    ):
        text = str(raw).strip()
        if text:
            ensure_dir(Path(text).expanduser().resolve().parent)

    return created


def _srgb_variant(texconv_format: str) -> str:
    mapping = {
        "R8G8B8A8_UNORM": "R8G8B8A8_UNORM_SRGB",
        "B8G8R8A8_UNORM": "B8G8R8A8_UNORM_SRGB",
        "BC1_UNORM": "BC1_UNORM_SRGB",
        "BC2_UNORM": "BC2_UNORM_SRGB",
        "BC3_UNORM": "BC3_UNORM_SRGB",
        "BC7_UNORM": "BC7_UNORM_SRGB",
    }
    return mapping.get(texconv_format, texconv_format)


def _linear_variant(texconv_format: str) -> str:
    mapping = {
        "R8G8B8A8_UNORM_SRGB": "R8G8B8A8_UNORM",
        "B8G8R8A8_UNORM_SRGB": "B8G8R8A8_UNORM",
        "BC1_UNORM_SRGB": "BC1_UNORM",
        "BC2_UNORM_SRGB": "BC2_UNORM",
        "BC3_UNORM_SRGB": "BC3_UNORM",
        "BC7_UNORM_SRGB": "BC7_UNORM",
    }
    return mapping.get(texconv_format, texconv_format)


def _normalize_alpha_policy(alpha_mode: str) -> str:
    normalized = str(alpha_mode or "").strip().lower()
    if normalized == "cutout":
        return "cutout_coverage"
    if normalized in {"none", "straight", "channel_data", "premultiplied"}:
        return normalized
    return "straight" if normalized else "none"


def _semantic_override_components(value: str) -> Tuple[str, str]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("Semantic override cannot be empty.")
    for separator in (":", "/"):
        if separator in normalized:
            texture_type, semantic_subtype = [piece.strip() for piece in normalized.split(separator, 1)]
            if texture_type not in _DEFAULT_SEMANTIC_SUBTYPES:
                raise ValueError(f"Unsupported semantic override texture type: {texture_type}")
            if not semantic_subtype:
                raise ValueError("Semantic override subtype cannot be empty.")
            return texture_type, semantic_subtype
    texture_type = _SEMANTIC_OVERRIDE_TEXTURE_TYPES.get(normalized)
    if texture_type is not None:
        if normalized in _DEFAULT_SEMANTIC_SUBTYPES:
            return normalized, _DEFAULT_SEMANTIC_SUBTYPES[normalized]
        return texture_type, normalized
    raise ValueError(f"Unsupported semantic override: {value}")


def _profile_for_key(key: str) -> TextureProcessingProfile:
    profile = _PROFILE_TABLE.get(str(key or "").strip().lower())
    if profile is None:
        raise ValueError(f"Unsupported texture processing profile: {key}")
    return profile


def _infer_profile_key(
    decision: TextureUpscaleDecision,
    alpha_policy: str,
    dds_info: DdsInfo,
    explicit_profile: Optional[str] = None,
) -> str:
    if explicit_profile:
        return _profile_for_key(explicit_profile).key

    if decision.precision_sensitive or decision.texture_type == "vector" or "FLOAT" in dds_info.texconv_format.upper() or "SNORM" in dds_info.texconv_format.upper():
        return "float_or_vector_preserve_only"
    if alpha_policy == "premultiplied":
        return "premultiplied_alpha_review_required"
    if decision.texture_type == "normal":
        return "normal_bc5"
    if decision.texture_type in {"height", "roughness"}:
        return "scalar_high_precision_bc4"
    if decision.texture_type == "mask":
        if decision.semantic_subtype in {"orm", "rma", "mra", "arm", "packed_mask", "material_mask", "material_response"} or decision.packed_channels:
            return "packed_mask_preserve_layout"
        if (
            not dds_info.has_alpha
            and alpha_policy == "none"
            and decision.semantic_subtype in _SCALAR_HIGH_PRECISION_MASK_SUBTYPES
        ):
            return "scalar_high_precision_bc4"
        return "scalar_bc4"
    if decision.texture_type == "ui" and alpha_policy in {"straight", "cutout_coverage"} and dds_info.has_alpha:
        return "ui_alpha"
    if alpha_policy == "cutout_coverage":
        return "color_cutout_alpha"
    return "color_default"


def _is_scalar_high_precision_candidate(
    decision: TextureUpscaleDecision,
    dds_info: DdsInfo,
    alpha_policy: str,
    profile: TextureProcessingProfile,
) -> bool:
    if profile.key != "scalar_high_precision_bc4":
        return False
    if dds_info.precision_sensitive or decision.precision_sensitive:
        return False
    if alpha_policy != "none":
        return False
    if decision.packed_channels:
        return False
    if decision.texture_type in {"height", "roughness"}:
        return True
    if decision.texture_type == "mask" and decision.semantic_subtype in _SCALAR_HIGH_PRECISION_MASK_SUBTYPES:
        return not dds_info.has_alpha
    return False


def _workflow_profile_for_rule(
    rule: Optional[TextureRule],
    workflow_profiles: Sequence[TextureWorkflowProfile],
) -> Optional[TextureWorkflowProfile]:
    if rule is None:
        return None
    target_id = str(getattr(rule, "workflow_profile_id", "") or "").strip()
    if not target_id:
        return None
    for profile in workflow_profiles:
        if profile.profile_id == target_id:
            return profile
    return None


def _effective_output_override(
    workflow_profile: Optional[TextureWorkflowProfile],
    rule: Optional[TextureRule],
) -> TextureWorkflowDdsOverride:
    override = TextureWorkflowDdsOverride()
    if workflow_profile is not None:
        override = TextureWorkflowDdsOverride(
            format_value=workflow_profile.format_value,
            size_value=workflow_profile.size_value,
            mip_value=workflow_profile.mip_value,
        )
    if rule is not None:
        if rule.format_value:
            override.format_value = rule.format_value
        if rule.size_value:
            override.size_value = rule.size_value
        if rule.mip_value:
            override.mip_value = rule.mip_value
    return override


def _effective_ncnn_settings(
    normalized: NormalizedConfig,
    workflow_profile: Optional[TextureWorkflowProfile],
) -> EffectiveNcnnSettings:
    settings = EffectiveNcnnSettings(
        model_name=normalized.ncnn_model_name,
        scale=normalized.ncnn_scale,
        tile_size=normalized.ncnn_tile_size,
        extra_args=normalized.ncnn_extra_args,
        post_correction_mode=normalized.upscale_post_correction_mode,
    )
    if workflow_profile is None:
        return settings
    if workflow_profile.ncnn_model_name:
        settings.model_name = workflow_profile.ncnn_model_name
    if workflow_profile.ncnn_scale is not None:
        settings.scale = workflow_profile.ncnn_scale
    if workflow_profile.ncnn_tile_size is not None:
        settings.tile_size = workflow_profile.ncnn_tile_size
    if workflow_profile.ncnn_extra_args:
        settings.extra_args = workflow_profile.ncnn_extra_args
    if workflow_profile.post_correction_mode:
        settings.post_correction_mode = workflow_profile.post_correction_mode
    return settings


def _apply_workflow_profile_action_override(
    workflow_profile: Optional[TextureWorkflowProfile],
    *,
    action: str,
    action_reason: str,
    requires_png_processing: bool,
) -> Tuple[str, str, bool]:
    if workflow_profile is None or not workflow_profile.action_mode:
        return action, action_reason, requires_png_processing

    profile_reason = f"workflow profile '{workflow_profile.label}' forces {workflow_profile.action_mode}"
    if workflow_profile.action_mode == "skip":
        return "skip_by_rule", profile_reason, False
    if workflow_profile.action_mode == "preserve_original":
        return "preserve_original", profile_reason, False
    if workflow_profile.action_mode == "rebuild_from_png":
        return "rebuild_from_png", profile_reason, True
    if workflow_profile.action_mode == "upscale_then_rebuild":
        return "upscale_then_rebuild", profile_reason, True
    return action, action_reason, requires_png_processing


def _decision_with_texture_rule_overrides(
    decision: TextureUpscaleDecision,
    rule: Optional[TextureRule],
    dds_info: DdsInfo,
    *,
    preset: str,
) -> TextureUpscaleDecision:
    if rule is None:
        return decision

    next_decision = decision
    notes = list(decision.notes)
    source_evidence = list(decision.source_evidence)

    if rule.semantic_value:
        override_type, override_subtype = _semantic_override_components(rule.semantic_value)
        next_decision = replace(
            next_decision,
            texture_type=override_type,
            semantic_subtype=override_subtype,
            should_upscale=should_upscale_texture(override_type, preset),
        )
        source_evidence.append(f"texture rule semantic override -> {override_type}/{override_subtype}")
        notes.append(f"texture rule overrides semantic classification to {override_type}/{override_subtype}.")

    if rule.colorspace_value:
        colorspace_value = str(rule.colorspace_value).strip().lower()
        target_colorspace = _dds_colorspace_intent_from_format(dds_info.texconv_format) if colorspace_value == "match_source" else colorspace_value
        next_decision = replace(next_decision, recommended_colorspace=target_colorspace or next_decision.recommended_colorspace)
        notes.append(f"texture rule overrides colorspace policy to {target_colorspace or 'match_source'}.")

    if rule.alpha_policy_value:
        alpha_override = str(rule.alpha_policy_value).strip().lower()
        mapped_alpha_mode = "cutout" if alpha_override == "cutout_coverage" else alpha_override
        next_decision = replace(
            next_decision,
            alpha_mode=mapped_alpha_mode,
            preserve_alpha=alpha_override != "none" and dds_info.has_alpha,
        )
        notes.append(f"texture rule overrides alpha policy to {alpha_override}.")

    if rule.intermediate_value:
        intermediate = str(rule.intermediate_value).strip().lower()
        preserve_original = intermediate == "technical_preserve_path"
        next_decision = replace(
            next_decision,
            intermediate_policy="preserve_original" if preserve_original else "png_ok",
            preserve_original_due_to_intermediate=preserve_original,
        )
        notes.append(f"texture rule overrides processing path to {intermediate}.")

    return replace(next_decision, notes=notes, source_evidence=source_evidence)


def _plan_path_kind(
    normalized: NormalizedConfig,
    decision: TextureUpscaleDecision,
    profile: TextureProcessingProfile,
    rule: Optional[TextureRule],
    dds_info: DdsInfo,
    alpha_policy: str,
) -> IntermediateKind | str:
    rule_intermediate = str(rule.intermediate_value).strip().lower() if rule is not None and rule.intermediate_value else ""
    unsafe_override_applies = (
        normalized.enable_unsafe_technical_override
        and is_technical_texture_type(decision.texture_type)
        and not (rule is not None and (rule.action == "skip" or rule_intermediate in {"technical_preserve_path", "technical_high_precision_path"}))
    )
    if rule is not None and rule.intermediate_value:
        return rule_intermediate
    if unsafe_override_applies:
        return "visible_color_png_path"
    if _is_scalar_high_precision_candidate(decision, dds_info, alpha_policy, profile):
        return "technical_high_precision_path"
    if profile.preserve_only or decision.preserve_original_due_to_intermediate:
        return "technical_preserve_path"
    if decision.texture_type in _VISIBLE_COLOR_TEXTURE_TYPES:
        return "visible_color_png_path"
    if decision.texture_type == "unknown" and not normalized.enable_automatic_texture_rules and decision.should_upscale:
        return "visible_color_png_path"
    return "technical_preserve_path"


def _build_backend_capability_matrix(
    normalized: NormalizedConfig,
    *,
    chain_analysis: Optional[ChainnerChainAnalysis] = None,
) -> BackendCapabilityMatrix:
    normalized_backend = str(normalized.upscale_backend or "").strip().lower()
    decisions_by_path_kind: Dict[str, BackendCapabilityDecision] = {
        "technical_preserve_path": BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="technical_preserve_path",
            compatible=True,
            execution_mode="preserve_original",
            reason="Pass 1 keeps this file on the technical preserve path instead of routing it through a PNG intermediate.",
        ),
        "technical_high_precision_path": BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="technical_high_precision_path",
            compatible=False,
            execution_mode="preserve_original",
            reason="Technical high-precision path requires a backend/path combination that has not been enabled for this run.",
        ),
    }
    planner_notes: List[str] = []
    if normalized.enable_unsafe_technical_override:
        planner_notes.append(
            "Expert unsafe technical override is enabled: technical maps may be forced through the generic visible-color PNG path instead of preserve/high-precision paths."
        )

    if normalized_backend == UPSCALE_BACKEND_NONE:
        visible_decision = BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="visible_color_png_path",
            compatible=True,
            execution_mode="rebuild_from_png",
            reason="Backend is disabled, so the planner will rebuild DDS from the current PNG input.",
        )
        if normalized.enable_dds_staging and normalized.dds_staging_root is not None:
            decisions_by_path_kind["technical_high_precision_path"] = BackendCapabilityDecision(
                backend=normalized_backend,
                path_kind="technical_high_precision_path",
                compatible=True,
                execution_mode="rebuild_from_high_precision_png",
                reason="Backend is disabled, but DDS staging is enabled, so eligible scalar technical maps can rebuild from high-precision staged PNG data.",
            )
        else:
            decisions_by_path_kind["technical_high_precision_path"] = BackendCapabilityDecision(
                backend=normalized_backend,
                path_kind="technical_high_precision_path",
                compatible=True,
                execution_mode="rebuild_from_high_precision_png",
                reason="Backend is disabled, so the technical high-precision path will use matching PNG files from PNG root when they are valid 16-bit grayscale intermediates.",
            )
    elif normalized_backend == UPSCALE_BACKEND_CHAINNER:
        if chain_analysis is not None and not chain_analysis.planner_compatible:
            preview_reasons = "; ".join(chain_analysis.blocking_warnings[:3])
            extra = "" if len(chain_analysis.blocking_warnings) <= 3 else f" (+{len(chain_analysis.blocking_warnings) - 3} more)"
            reason = (
                "chaiNNer is not planner-compatible for the visible-color path with the current chain configuration: "
                f"{preview_reasons}{extra}"
            )
            visible_decision = BackendCapabilityDecision(
                backend=normalized_backend,
                path_kind="visible_color_png_path",
                compatible=False,
                execution_mode="preserve_original",
                reason=reason,
            )
            planner_notes.extend(chain_analysis.blocking_warnings[:5])
        else:
            reason = (
                "chaiNNer is allowed on the visible-color path when the chain reads from the planned input roots "
                "and writes planner-selected PNG outputs into the configured PNG root."
            )
            if chain_analysis is None:
                reason += " Runtime chain validation is still pending."
            visible_decision = BackendCapabilityDecision(
                backend=normalized_backend,
                path_kind="visible_color_png_path",
                compatible=True,
                execution_mode="upscale_then_rebuild",
                reason=reason,
            )
        decisions_by_path_kind["technical_high_precision_path"] = BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="technical_high_precision_path",
            compatible=False,
            execution_mode="preserve_original",
            reason="chaiNNer is only trusted on the visible-color path in this tranche. Technical high-precision scalar paths stay preserve-first.",
        )
    elif normalized_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
        visible_decision = BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="visible_color_png_path",
            compatible=True,
            execution_mode="upscale_then_rebuild",
            reason=f"{normalized_backend} is allowed on the visible-color path in this tranche.",
        )
        decisions_by_path_kind["technical_high_precision_path"] = BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="technical_high_precision_path",
            compatible=False,
            execution_mode="preserve_original",
            reason=f"{normalized_backend} does not support the technical high-precision path in this tranche.",
        )
    else:
        visible_decision = BackendCapabilityDecision(
            backend=normalized_backend,
            path_kind="visible_color_png_path",
            compatible=False,
            execution_mode="preserve_original",
            reason=f"Unsupported upscale backend: {normalized_backend}",
        )

    decisions_by_path_kind["visible_color_png_path"] = visible_decision
    return BackendCapabilityMatrix(
        backend=normalized_backend,
        decisions_by_path_kind=decisions_by_path_kind,
        planner_notes=tuple(planner_notes),
    )


def _resolve_backend_capability(
    backend_matrix: BackendCapabilityMatrix,
    path_kind: str,
) -> BackendCapabilityDecision:
    return backend_matrix.decision_for(path_kind)


def _plan_preserve_reason(
    decision: TextureUpscaleDecision,
    profile: TextureProcessingProfile,
    path_kind: str,
    backend_capability: BackendCapabilityDecision,
    rule: Optional[TextureRule],
) -> str:
    if rule is not None and rule.action == "skip":
        return f"texture rule matched: {rule.pattern} -> skip"
    if rule is not None and rule.intermediate_value == "technical_preserve_path":
        return f"texture rule forces technical preserve path for {decision.texture_type}/{decision.semantic_subtype}"
    if rule is not None and rule.intermediate_value == "technical_high_precision_path" and not backend_capability.compatible:
        return backend_capability.reason
    if not backend_capability.compatible:
        return backend_capability.reason
    if path_kind not in {"technical_preserve_path", "technical_high_precision_path"}:
        return ""
    if profile.preserve_only:
        return f"profile {profile.key} is preserve-only for {decision.texture_type}/{decision.semantic_subtype}"
    if decision.preserve_original_due_to_intermediate:
        return f"automatic rules preserve {decision.texture_type}/{decision.semantic_subtype}"
    if path_kind == "technical_high_precision_path":
        return ""
    return f"planner preserved {decision.texture_type}/{decision.semantic_subtype} on the technical preserve path"


def _lossy_intermediate_warning(decision: TextureUpscaleDecision, path_kind: str) -> str:
    if path_kind != "visible_color_png_path":
        return ""
    if decision.intermediate_policy == "risky_png":
        return f"Visible-color path still uses a lossy PNG intermediate for {decision.texture_type}/{decision.semantic_subtype}; review the rebuilt DDS carefully."
    return ""


def describe_processing_path_kind(path_kind: str) -> str:
    normalized = str(path_kind or "").strip().lower()
    if normalized == "visible_color_png_path":
        return "Visible-color PNG path: generic 8-bit image staging for color-like textures."
    if normalized == "technical_preserve_path":
        return "Technical preserve path: keep the original DDS unchanged because the current workflow is not trusted for this texture."
    if normalized == "technical_high_precision_path":
        return "Technical high-precision path: use high-bit-depth staged PNG data for eligible scalar technical textures instead of the generic visible-color path."
    return f"Unknown planner path: {path_kind}"


def _build_texture_processing_plan_entry(
    normalized: NormalizedConfig,
    dds_path: Path,
    rel_path: Path,
    dds_info: DdsInfo,
    decision: TextureUpscaleDecision,
    rule: Optional[TextureRule],
    backend_matrix: BackendCapabilityMatrix,
) -> TextureProcessingPlan:
    decision = _decision_with_texture_rule_overrides(
        decision,
        rule,
        dds_info,
        preset=normalized.upscale_texture_preset,
    )
    rule_intermediate = str(rule.intermediate_value).strip().lower() if rule is not None and rule.intermediate_value else ""
    unsafe_override_applies = (
        normalized.enable_unsafe_technical_override
        and is_technical_texture_type(decision.texture_type)
        and not (rule is not None and (rule.action == "skip" or rule_intermediate in {"technical_preserve_path", "technical_high_precision_path"}))
    )
    workflow_profile = _workflow_profile_for_rule(rule, normalized.workflow_profiles)
    effective_output_override = _effective_output_override(workflow_profile, rule)
    effective_ncnn_settings = _effective_ncnn_settings(normalized, workflow_profile)
    base_alpha_policy = _normalize_alpha_policy(decision.alpha_mode)
    profile = _profile_for_key(_infer_profile_key(decision, base_alpha_policy, dds_info, rule.profile_value if rule else None))
    alpha_policy = str(rule.alpha_policy_value).strip().lower() if rule and rule.alpha_policy_value else profile.alpha_policy or base_alpha_policy
    path_kind = _plan_path_kind(normalized, decision, profile, rule, dds_info, alpha_policy)
    if (
        unsafe_override_applies
        and path_kind == "visible_color_png_path"
    ):
        notes = list(decision.notes)
        notes.append(
            "expert override forced this technical texture onto the generic visible-color PNG path; expect a higher risk of broken normals, mask drift, or shading errors."
        )
        decision = replace(decision, notes=notes)
    if path_kind == "technical_high_precision_path" and decision.preserve_original_due_to_intermediate:
        notes = list(decision.notes)
        notes.append("planner upgraded this scalar technical map from preserve-only to the technical high-precision path.")
        decision = replace(
            decision,
            preserve_original_due_to_intermediate=False,
            intermediate_policy="high_precision_png",
            notes=notes,
        )
    backend_capability = _resolve_backend_capability(backend_matrix, path_kind)
    preserve_reason = _plan_preserve_reason(decision, profile, path_kind, backend_capability, rule)
    lossy_warning = _lossy_intermediate_warning(decision, path_kind)

    dds_info.colorspace_intent = _dds_colorspace_intent_from_format(dds_info.texconv_format)
    dds_info.precision_sensitive = dds_info.precision_sensitive or decision.precision_sensitive
    dds_info.packed_channel_risk = bool(decision.packed_channels) or decision.semantic_subtype in {"orm", "rma", "mra", "arm", "packed_mask", "material_mask", "material_response"}
    dds_info.preserve_only_source = bool(preserve_reason) or profile.preserve_only

    if rule is not None and rule.action == "skip":
        action = "skip_by_rule"
        action_reason = preserve_reason
        requires_png_processing = False
    elif (
        normalized.upscale_backend == UPSCALE_BACKEND_NONE
        and backend_capability.compatible
        and path_kind in {"visible_color_png_path", "technical_high_precision_path"}
    ):
        action = backend_capability.execution_mode
        action_reason = backend_capability.reason
        requires_png_processing = action in {"rebuild_from_png", "rebuild_from_high_precision_png"}
    elif not decision.should_upscale and not unsafe_override_applies:
        action = "preserve_original"
        action_reason = f"preset excludes {decision.texture_type}/{decision.semantic_subtype}"
        preserve_reason = action_reason
        requires_png_processing = False
    elif path_kind == "technical_preserve_path" or not backend_capability.compatible:
        action = "preserve_original"
        action_reason = preserve_reason or backend_capability.reason
        requires_png_processing = False
    else:
        action = backend_capability.execution_mode
        action_reason = backend_capability.reason
        requires_png_processing = action in {"rebuild_from_png", "rebuild_from_high_precision_png", "upscale_then_rebuild"}

    action, action_reason, requires_png_processing = _apply_workflow_profile_action_override(
        workflow_profile,
        action=action,
        action_reason=action_reason,
        requires_png_processing=requires_png_processing,
    )
    if action in {"preserve_original", "skip_by_rule"} and action_reason:
        preserve_reason = action_reason

    return TextureProcessingPlan(
        dds_path=dds_path,
        relative_path=rel_path,
        dds_info=dds_info,
        decision=decision,
        action=action,
        action_reason=action_reason,
        path_kind=path_kind,
        intermediate_kind=path_kind,
        profile=profile,
        alpha_policy=alpha_policy,
        backend_capability=backend_capability,
        requires_png_processing=requires_png_processing,
        preserve_reason=preserve_reason,
        lossy_intermediate_warning=lossy_warning,
        matched_rule=rule,
        workflow_profile=workflow_profile,
        effective_output_override=effective_output_override,
        effective_ncnn_settings=effective_ncnn_settings,
        semantic_evidence=TextureSemanticEvidence(tuple(decision.source_evidence)),
    )


def build_single_texture_processing_plan(
    normalized: NormalizedConfig,
    dds_path: Path,
    *,
    relative_path: Optional[Path] = None,
    decision: Optional[TextureUpscaleDecision] = None,
    backend_matrix: Optional[BackendCapabilityMatrix] = None,
) -> TextureProcessingPlan:
    resolved_relative = relative_path or dds_path.relative_to(normalized.original_dds_root)
    dds_info = parse_dds(dds_path)
    resolved_decision = decision or suggest_texture_upscale_decision(
        resolved_relative.as_posix(),
        preset=normalized.upscale_texture_preset,
        original_texconv_format=dds_info.texconv_format,
        has_alpha=dds_info.has_alpha,
        enable_automatic_rules=normalized.enable_automatic_texture_rules,
    )
    rule = find_matching_texture_rule(resolved_relative, normalized.texture_rules)
    resolved_backend_matrix = backend_matrix or _build_backend_capability_matrix(normalized)
    return _build_texture_processing_plan_entry(
        normalized,
        dds_path,
        resolved_relative,
        dds_info,
        resolved_decision,
        rule,
        resolved_backend_matrix,
    )


def apply_texture_workflow_output_override(
    settings: DdsOutputSettings,
    override: TextureWorkflowDdsOverride,
    *,
    dds_info: DdsInfo,
    note_label: str,
) -> DdsOutputSettings:
    next_settings = DdsOutputSettings(
        texconv_format=settings.texconv_format,
        mip_count=settings.mip_count,
        width=settings.width,
        height=settings.height,
        resize_to_dimensions=settings.resize_to_dimensions,
        notes=list(settings.notes),
        texconv_color_args=list(settings.texconv_color_args),
        texconv_extra_args=list(settings.texconv_extra_args),
    )

    if override.format_value:
        if override.format_value == DDS_FORMAT_MODE_MATCH_ORIGINAL:
            next_settings.texconv_format = dds_info.texconv_format
        else:
            next_settings.texconv_format = override.format_value
    if override.size_value:
        if override.size_value == DDS_SIZE_MODE_PNG:
            next_settings.resize_to_dimensions = False
        elif override.size_value == DDS_SIZE_MODE_ORIGINAL:
            next_settings.width = dds_info.width
            next_settings.height = dds_info.height
            next_settings.resize_to_dimensions = True
        else:
            width_text, height_text = override.size_value.lower().split("x", 1)
            next_settings.width = int(width_text)
            next_settings.height = int(height_text)
            next_settings.resize_to_dimensions = True
    if override.mip_value:
        max_possible_mips = max_mips_for_size(next_settings.width, next_settings.height)
        if override.mip_value == DDS_MIP_MODE_MATCH_ORIGINAL:
            next_settings.mip_count = min(dds_info.mip_count, max_possible_mips)
        elif override.mip_value == DDS_MIP_MODE_FULL_CHAIN:
            next_settings.mip_count = max_mips_for_size(next_settings.width, next_settings.height)
        elif override.mip_value == DDS_MIP_MODE_SINGLE:
            next_settings.mip_count = 1
        elif override.mip_value not in {DDS_MIP_MODE_MATCH_ORIGINAL, DDS_MIP_MODE_FULL_CHAIN, DDS_MIP_MODE_SINGLE}:
            next_settings.mip_count = int(override.mip_value)

    if override.format_value or override.size_value or override.mip_value:
        next_settings.notes.append(note_label)
    return next_settings


def _resolve_plan_output_settings(
    normalized: NormalizedConfig,
    plan: TextureProcessingPlan,
    png_width: int,
    png_height: int,
    *,
    has_alpha: bool,
) -> DdsOutputSettings:
    output_settings = resolve_dds_output_settings(normalized, plan.dds_info, png_width, png_height)
    explicit_output_format_override = str(plan.effective_output_override.format_value or "").strip().lower()
    if plan.workflow_profile is not None and (
        plan.effective_output_override.format_value
        or plan.effective_output_override.size_value
        or plan.effective_output_override.mip_value
    ):
        output_settings = apply_texture_workflow_output_override(
            output_settings,
            plan.effective_output_override,
            dds_info=plan.dds_info,
            note_label=f"workflow profile matched: {plan.workflow_profile.label}",
        )
    elif plan.effective_output_override.format_value or plan.effective_output_override.size_value or plan.effective_output_override.mip_value:
        output_settings = apply_texture_workflow_output_override(
            output_settings,
            plan.effective_output_override,
            dds_info=plan.dds_info,
            note_label="workflow override applied",
        )
    explicit_profile_override = bool(
        plan.matched_rule is not None and str(plan.matched_rule.profile_value or "").strip()
    )
    if normalized.enable_automatic_texture_rules:
        output_settings = apply_automatic_texture_rule_adjustments(
            output_settings,
            plan.relative_path,
            plan.dds_info,
            has_alpha=has_alpha,
            preset=normalized.upscale_texture_preset,
            intermediate_kind=plan.path_kind,
            semantic_decision=plan.decision,
            allow_auto_format_override=(
                plan.path_kind == "technical_high_precision_path" and explicit_output_format_override == ""
            ),
            prefer_manual_visible_format=(
                normalized.dds_format_mode == DDS_FORMAT_MODE_MATCH_ORIGINAL
                and explicit_output_format_override == ""
                and not explicit_profile_override
            ),
        )

    allow_profile_format_override = (
        plan.profile.preferred_texconv_format not in {"", "MATCH_ORIGINAL"}
        and explicit_output_format_override == ""
        and (
            explicit_profile_override
            or plan.path_kind == "technical_high_precision_path"
        )
    )
    if allow_profile_format_override:
        output_settings.texconv_format = plan.profile.preferred_texconv_format
    elif (
        plan.profile.preferred_texconv_format not in {"", "MATCH_ORIGINAL"}
        and normalized.dds_format_mode == DDS_FORMAT_MODE_MATCH_ORIGINAL
        and not normalized.enable_automatic_texture_rules
        and explicit_output_format_override == ""
        and not explicit_profile_override
    ):
        output_settings.notes.append(
            f"planner profile suggests {plan.profile.preferred_texconv_format}, but manual Match original DDS format remains in effect because automatic color/format rules are disabled."
        )
    elif (
        plan.profile.preferred_texconv_format not in {"", "MATCH_ORIGINAL"}
        and plan.path_kind != "technical_high_precision_path"
        and explicit_output_format_override == ""
        and not explicit_profile_override
        and output_settings.texconv_format != plan.profile.preferred_texconv_format
    ):
        output_settings.notes.append(
            f"planner profile suggests {plan.profile.preferred_texconv_format}, but the explicit DDS Output format setting remains in effect."
        )
    output_settings.notes.append(f"planner profile: {plan.profile.key}")
    output_settings.notes.append(f"planner path: {plan.path_kind}")
    output_settings.notes.append(f"planner alpha policy: {plan.alpha_policy}")
    if plan.path_kind == "technical_high_precision_path":
        output_settings.notes.append(
            "planner path detail: expects a 16-bit grayscale-style PNG intermediate and falls back to preserving the original DDS if that intermediate is missing or invalid."
        )
    if plan.lossy_intermediate_warning:
        output_settings.notes.append(plan.lossy_intermediate_warning)
    return output_settings


def apply_automatic_texture_rule_adjustments(
    output_settings: DdsOutputSettings,
    rel_path: Path,
    dds_info: DdsInfo,
    *,
    has_alpha: bool,
    preset: str,
    intermediate_kind: str = "visible_color_png_path",
    sidecar_texts: Sequence[str] = (),
    semantic_decision: Optional[TextureUpscaleDecision] = None,
    allow_auto_format_override: bool = True,
    prefer_manual_visible_format: bool = False,
) -> DdsOutputSettings:
    decision = semantic_decision or suggest_texture_upscale_decision(
        rel_path.as_posix(),
        preset=preset,
        original_texconv_format=dds_info.texconv_format,
        has_alpha=has_alpha,
        sidecar_texts=sidecar_texts,
        enable_automatic_rules=True,
    )
    next_settings = DdsOutputSettings(
        texconv_format=output_settings.texconv_format,
        mip_count=output_settings.mip_count,
        width=output_settings.width,
        height=output_settings.height,
        resize_to_dimensions=output_settings.resize_to_dimensions,
        notes=list(output_settings.notes),
        texconv_color_args=list(output_settings.texconv_color_args),
        texconv_extra_args=list(output_settings.texconv_extra_args),
    )
    current_format = next_settings.texconv_format.upper()
    recommended = decision.recommended_texconv_format.upper()
    preserve_visible_format = (
        prefer_manual_visible_format
        and decision.texture_type in _VISIBLE_COLOR_TEXTURE_TYPES
    )

    updated_format = current_format
    if decision.texture_type in {"color", "ui", "emissive", "impostor"}:
        if allow_auto_format_override and not preserve_visible_format:
            srgb_candidate = _srgb_variant(current_format)
            if srgb_candidate != current_format:
                updated_format = srgb_candidate
            elif current_format == dds_info.texconv_format.upper() and recommended.endswith("_SRGB"):
                updated_format = recommended
    elif decision.texture_type == "normal" and allow_auto_format_override:
        if current_format.endswith("_SRGB") or current_format not in {"BC5_UNORM", "BC5_SNORM"}:
            updated_format = recommended
    elif decision.texture_type == "height" and allow_auto_format_override:
        linear_candidate = _linear_variant(current_format)
        if "FLOAT" in dds_info.texconv_format.upper():
            updated_format = dds_info.texconv_format.upper()
        elif linear_candidate != current_format:
            updated_format = linear_candidate
        elif current_format.endswith("_SRGB") or current_format not in {"BC4_UNORM", "BC4_SNORM", "R8G8B8A8_UNORM", "B8G8R8A8_UNORM"}:
            updated_format = recommended
    elif decision.texture_type == "vector" and allow_auto_format_override:
        linear_candidate = _linear_variant(current_format)
        if "FLOAT" in dds_info.texconv_format.upper():
            updated_format = dds_info.texconv_format.upper()
        elif linear_candidate != current_format:
            updated_format = linear_candidate
        elif current_format.endswith("_SRGB") or current_format != dds_info.texconv_format.upper():
            updated_format = recommended
    elif decision.texture_type == "roughness" and allow_auto_format_override:
        if current_format.endswith("_SRGB") or current_format not in {"BC4_UNORM", "BC4_SNORM"}:
            updated_format = recommended
    elif decision.texture_type == "mask" and allow_auto_format_override:
        linear_candidate = _linear_variant(current_format)
        if linear_candidate != current_format:
            updated_format = linear_candidate
        elif current_format.endswith("_SRGB"):
            updated_format = recommended

    if updated_format != current_format:
        next_settings.texconv_format = updated_format
        next_settings.notes.append(
            f"automatic texture rule: {decision.texture_type}/{decision.semantic_subtype} -> {updated_format}"
        )
    elif not allow_auto_format_override:
        next_settings.notes.append(
            f"automatic texture rule: keeping explicit DDS Output format {current_format}; safety rules are limited to path, alpha, and colorspace handling for this file."
        )
    elif preserve_visible_format:
        next_settings.notes.append(
            f"automatic texture rule: preserved original visible-texture format {current_format} to avoid unintended luminance shifts under manual Match original DDS format."
        )
    next_settings.texconv_color_args.clear()
    if decision.recommended_colorspace == "linear":
        next_settings.texconv_color_args.extend(["--ignore-srgb"])
    elif decision.recommended_colorspace == "srgb":
        next_settings.notes.append(
            "auto-rule: visible texture rebuild keeps PNG pixel values as-is and avoids extra texconv sRGB conversion flags to reduce luminance drift."
        )

    next_settings.texconv_extra_args = [
        arg
        for arg in next_settings.texconv_extra_args
        if str(arg).strip().lower() not in {
            "-sepalpha",
            "--separate-alpha",
            "--keep-coverage",
            "-pmalpha",
            "--premultiplied-alpha",
        }
    ]
    if decision.alpha_mode in {"cutout"} and next_settings.mip_count > 1:
        next_settings.texconv_extra_args.extend(["--keep-coverage", "0.5"])
        next_settings.notes.append("auto-rule: alpha-tested cutout texture will preserve alpha coverage during mip generation.")
    if decision.alpha_mode == "channel_data" and decision.preserve_alpha:
        next_settings.texconv_extra_args.append("--separate-alpha")
        next_settings.notes.append("auto-rule: alpha channel appears to store data, so separate-alpha mip handling is enabled.")
    if decision.alpha_mode == "premultiplied":
        next_settings.notes.append("auto-rule: possible premultiplied alpha detected; verify final blend behavior manually.")

    for note in decision.notes:
        prefixed = f"auto-rule: {note}"
        if prefixed not in next_settings.notes:
            next_settings.notes.append(prefixed)
    if decision.texture_type in {"height", "vector"}:
        if output_settings.width != dds_info.width or output_settings.height != dds_info.height:
            next_settings.notes.append(
                f"auto-rule: {decision.texture_type} map is using resized PNG dimensions; verify that the semantic data still makes sense."
            )
        if intermediate_kind == "visible_color_png_path" and is_png_intermediate_high_risk(decision.texture_type, dds_info.texconv_format):
            next_settings.notes.append(
                f"auto-rule: {decision.texture_type} map may lose precision through PNG intermediates; compare carefully against the source."
            )
        elif intermediate_kind == "technical_high_precision_path":
            next_settings.notes.append(
                f"auto-rule: {decision.texture_type} map is using the technical high-precision path instead of the generic visible-color PNG path."
            )
    if decision.semantic_subtype in {"orm", "rma", "mra", "arm", "packed_mask", "opacity_mask"}:
        next_settings.notes.append(
            f"auto-rule: packed-channel semantic '{decision.semantic_subtype}' detected; preserve exact channel meaning when reviewing results."
        )
    return next_settings


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
        texconv_color_args=list(settings.texconv_color_args),
        texconv_extra_args=list(settings.texconv_extra_args),
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


def _rule_matches_path(pattern: str, relative_path: Path, *, match_mode: str = "glob") -> bool:
    rel_posix = relative_path.as_posix().lower()
    basename = relative_path.name.lower()
    normalized_pattern = pattern.replace("\\", "/").strip().lower()
    if not normalized_pattern:
        return False
    if match_mode == "exact":
        return rel_posix == normalized_pattern
    return fnmatch.fnmatch(rel_posix, normalized_pattern) or fnmatch.fnmatch(basename, normalized_pattern)


def find_matching_texture_rule(relative_path: Path, rules: Sequence[TextureRule]) -> Optional[TextureRule]:
    for rule in reversed(tuple(rules)):
        if not bool(getattr(rule, "enabled", True)):
            continue
        if _rule_matches_path(rule.pattern, relative_path, match_mode=str(getattr(rule, "match_mode", "glob") or "glob")):
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
            elif lowered_key in {"semantic", "semantics"}:
                _semantic_override_components(value)
                rule.semantic_value = lowered_value
            elif lowered_key == "profile":
                _profile_for_key(lowered_value)
                rule.profile_value = lowered_value
            elif lowered_key == "colorspace":
                if lowered_value not in _VALID_RULE_COLORSPACE_OVERRIDES:
                    raise ValueError(f"Texture rule line {line_number} has an invalid colorspace override: {value}")
                rule.colorspace_value = lowered_value
            elif lowered_key in {"alpha", "alpha_policy"}:
                if lowered_value not in _VALID_RULE_ALPHA_POLICIES:
                    raise ValueError(f"Texture rule line {line_number} has an invalid alpha policy override: {value}")
                rule.alpha_policy_value = lowered_value
            elif lowered_key in {"intermediate", "path"}:
                if lowered_value not in _VALID_RULE_INTERMEDIATE_OVERRIDES:
                    raise ValueError(f"Texture rule line {line_number} has an invalid intermediate override: {value}")
                rule.intermediate_value = lowered_value
            else:
                raise ValueError(f"Texture rule line {line_number} has an unknown key: {key}")

        rules.append(rule)

    return tuple(rules)


def _normalize_rule_match_mode(value: object) -> str:
    normalized = str(value or "").strip().lower() or "glob"
    if normalized not in _VALID_RULE_MATCH_MODES:
        raise ValueError(f"Unsupported texture rule match mode: {value}")
    return normalized


def _normalize_workflow_action_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "inherit":
        normalized = ""
    if normalized not in _VALID_WORKFLOW_PROFILE_ACTIONS:
        raise ValueError(f"Unsupported workflow profile action mode: {value}")
    return normalized


def _coerce_optional_positive_int(value: object, field_name: str) -> Optional[int]:
    if value in (None, "", False):
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field_name} must be 0 or greater.")
    return parsed


def coerce_texture_workflow_profiles(raw_value: object) -> Tuple[TextureWorkflowProfile, ...]:
    if raw_value in (None, "", ()):
        return ()
    items = raw_value
    if isinstance(items, TextureWorkflowProfile):
        items = (items,)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError("Workflow profiles must be a sequence.")

    profiles: List[TextureWorkflowProfile] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if isinstance(item, TextureWorkflowProfile):
            profile = TextureWorkflowProfile(
                profile_id=str(item.profile_id or "").strip(),
                label=str(item.label or "").strip(),
                action_mode=_normalize_workflow_action_mode(item.action_mode),
                format_value=str(item.format_value or "").strip() or None,
                size_value=str(item.size_value or "").strip().lower() or None,
                mip_value=str(item.mip_value or "").strip().lower() or None,
                ncnn_model_name=str(item.ncnn_model_name or "").strip(),
                ncnn_scale=_coerce_optional_positive_int(item.ncnn_scale, "Workflow profile NCNN scale"),
                ncnn_tile_size=_coerce_optional_positive_int(item.ncnn_tile_size, "Workflow profile NCNN tile size"),
                ncnn_extra_args=str(item.ncnn_extra_args or "").strip(),
                post_correction_mode=str(item.post_correction_mode or "").strip().lower(),
            )
        elif isinstance(item, Mapping):
            profile = TextureWorkflowProfile(
                profile_id=str(item.get("profile_id", "") or "").strip(),
                label=str(item.get("label", "") or "").strip(),
                action_mode=_normalize_workflow_action_mode(item.get("action_mode", "")),
                format_value=str(item.get("format_value", "") or "").strip() or None,
                size_value=str(item.get("size_value", "") or "").strip().lower() or None,
                mip_value=str(item.get("mip_value", "") or "").strip().lower() or None,
                ncnn_model_name=str(item.get("ncnn_model_name", "") or "").strip(),
                ncnn_scale=_coerce_optional_positive_int(item.get("ncnn_scale"), "Workflow profile NCNN scale"),
                ncnn_tile_size=_coerce_optional_positive_int(item.get("ncnn_tile_size"), "Workflow profile NCNN tile size"),
                ncnn_extra_args=str(item.get("ncnn_extra_args", "") or "").strip(),
                post_correction_mode=str(item.get("post_correction_mode", "") or "").strip().lower(),
            )
        else:
            raise ValueError(f"Workflow profile {index} is invalid.")

        if not profile.profile_id:
            raise ValueError(f"Workflow profile {index} is missing profile_id.")
        if not profile.label:
            raise ValueError(f"Workflow profile {index} is missing label.")
        if profile.profile_id in seen_ids:
            raise ValueError(f"Workflow profile id '{profile.profile_id}' is duplicated.")
        if profile.format_value and profile.format_value not in {DDS_FORMAT_MODE_MATCH_ORIGINAL, *SUPPORTED_TEXCONV_FORMAT_CHOICES}:
            raise ValueError(f"Workflow profile '{profile.label}' has an unsupported DDS format override: {profile.format_value}")
        if profile.size_value:
            if profile.size_value not in {DDS_SIZE_MODE_PNG, DDS_SIZE_MODE_ORIGINAL} and not re.match(r"^\d+x\d+$", profile.size_value):
                raise ValueError(f"Workflow profile '{profile.label}' has an invalid DDS size override: {profile.size_value}")
        if profile.mip_value:
            if profile.mip_value not in {DDS_MIP_MODE_MATCH_ORIGINAL, DDS_MIP_MODE_FULL_CHAIN, DDS_MIP_MODE_SINGLE} and not (profile.mip_value.isdigit() and int(profile.mip_value) >= 1):
                raise ValueError(f"Workflow profile '{profile.label}' has an invalid DDS mip override: {profile.mip_value}")
        if profile.ncnn_scale is not None and profile.ncnn_scale not in {2, 3, 4}:
            raise ValueError(f"Workflow profile '{profile.label}' has an invalid NCNN scale override: {profile.ncnn_scale}")
        if profile.post_correction_mode:
            _validate_choice(
                profile.post_correction_mode,
                (
                    UPSCALE_POST_CORRECTION_NONE,
                    UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
                    UPSCALE_POST_CORRECTION_MATCH_LEVELS,
                    UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
                    UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
                    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
                    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
                ),
                "workflow profile post-upscale correction mode",
            )
        profiles.append(profile)
        seen_ids.add(profile.profile_id)
    return tuple(profiles)


def coerce_texture_workflow_rules(raw_value: object) -> Tuple[TextureRule, ...]:
    if raw_value in (None, "", ()):
        return ()
    items = raw_value
    if isinstance(items, TextureRule):
        items = (items,)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError("Texture workflow rules must be a sequence.")

    rules: List[TextureRule] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, TextureRule):
            rule = TextureRule(
                pattern=str(item.pattern or "").strip(),
                action=str(item.action or "process").strip().lower() or "process",
                format_value=str(item.format_value or "").strip() or None,
                size_value=str(item.size_value or "").strip().lower() or None,
                mip_value=str(item.mip_value or "").strip().lower() or None,
                semantic_value=str(item.semantic_value or "").strip().lower() or None,
                profile_value=str(item.profile_value or "").strip().lower() or None,
                colorspace_value=str(item.colorspace_value or "").strip().lower() or None,
                alpha_policy_value=str(item.alpha_policy_value or "").strip().lower() or None,
                intermediate_value=str(item.intermediate_value or "").strip().lower() or None,
                enabled=bool(getattr(item, "enabled", True)),
                match_mode=_normalize_rule_match_mode(getattr(item, "match_mode", "glob")),
                workflow_profile_id=str(getattr(item, "workflow_profile_id", "") or "").strip(),
                source_line=str(item.source_line or "").strip(),
            )
        elif isinstance(item, Mapping):
            rule = TextureRule(
                pattern=str(item.get("pattern", "") or "").strip(),
                action=str(item.get("action", "process") or "process").strip().lower() or "process",
                format_value=str(item.get("format_value", "") or "").strip() or None,
                size_value=str(item.get("size_value", "") or "").strip().lower() or None,
                mip_value=str(item.get("mip_value", "") or "").strip().lower() or None,
                semantic_value=str(item.get("semantic_value", "") or "").strip().lower() or None,
                profile_value=str(item.get("profile_value", "") or "").strip().lower() or None,
                colorspace_value=str(item.get("colorspace_value", "") or "").strip().lower() or None,
                alpha_policy_value=str(item.get("alpha_policy_value", "") or "").strip().lower() or None,
                intermediate_value=str(item.get("intermediate_value", "") or "").strip().lower() or None,
                enabled=bool(item.get("enabled", True)),
                match_mode=_normalize_rule_match_mode(item.get("match_mode", "glob")),
                workflow_profile_id=str(item.get("workflow_profile_id", "") or "").strip(),
                source_line=str(item.get("source_line", "") or "").strip(),
            )
        else:
            raise ValueError(f"Texture workflow rule {index} is invalid.")

        if not rule.pattern:
            raise ValueError(f"Texture workflow rule {index} is missing a pattern.")
        if rule.action not in {"process", "skip"}:
            raise ValueError(f"Texture workflow rule '{rule.pattern}' has an invalid legacy action: {rule.action}")
        if rule.semantic_value:
            _semantic_override_components(rule.semantic_value)
        if rule.profile_value:
            _profile_for_key(rule.profile_value)
        if rule.colorspace_value and rule.colorspace_value not in _VALID_RULE_COLORSPACE_OVERRIDES:
            raise ValueError(f"Texture workflow rule '{rule.pattern}' has an invalid colorspace override: {rule.colorspace_value}")
        if rule.alpha_policy_value and rule.alpha_policy_value not in _VALID_RULE_ALPHA_POLICIES:
            raise ValueError(f"Texture workflow rule '{rule.pattern}' has an invalid alpha policy override: {rule.alpha_policy_value}")
        if rule.intermediate_value and rule.intermediate_value not in _VALID_RULE_INTERMEDIATE_OVERRIDES:
            raise ValueError(f"Texture workflow rule '{rule.pattern}' has an invalid intermediate override: {rule.intermediate_value}")
        rules.append(rule)
    return tuple(rules)


def _make_unique_workflow_profile_id(seed: str, existing_ids: set[str], *, fallback_prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(seed or "").strip().lower()).strip("_") or fallback_prefix
    candidate = normalized
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{normalized}_{suffix}"
        suffix += 1
    existing_ids.add(candidate)
    return candidate


def migrate_legacy_texture_rules_to_structured(raw_text: str) -> Tuple[Tuple[TextureWorkflowProfile, ...], Tuple[TextureRule, ...]]:
    legacy_rules = parse_texture_rules(raw_text)
    if not legacy_rules:
        return (), ()

    profiles: List[TextureWorkflowProfile] = []
    rules: List[TextureRule] = []
    existing_profile_ids: set[str] = set()

    for index, legacy_rule in enumerate(legacy_rules, start=1):
        workflow_profile_id = ""
        if (
            legacy_rule.action == "skip"
            or legacy_rule.format_value
            or legacy_rule.size_value
            or legacy_rule.mip_value
        ):
            label_seed = legacy_rule.pattern or f"Imported Rule {index}"
            workflow_profile_id = _make_unique_workflow_profile_id(
                f"imported_{label_seed}",
                existing_profile_ids,
                fallback_prefix=f"imported_rule_{index}",
            )
            profiles.append(
                TextureWorkflowProfile(
                    profile_id=workflow_profile_id,
                    label=f"Imported Rule {index}",
                    action_mode="skip" if legacy_rule.action == "skip" else "",
                    format_value=legacy_rule.format_value,
                    size_value=legacy_rule.size_value,
                    mip_value=legacy_rule.mip_value,
                )
            )
        rules.append(
            TextureRule(
                pattern=legacy_rule.pattern,
                semantic_value=legacy_rule.semantic_value,
                profile_value=legacy_rule.profile_value,
                colorspace_value=legacy_rule.colorspace_value,
                alpha_policy_value=legacy_rule.alpha_policy_value,
                intermediate_value=legacy_rule.intermediate_value,
                enabled=True,
                match_mode="glob",
                workflow_profile_id=workflow_profile_id,
                source_line=legacy_rule.source_line or legacy_rule.pattern,
            )
        )

    return tuple(profiles), tuple(rules)


def resolve_default_staging_png_root(png_root: Path, use_separate_output_root: bool) -> Path:
    if not use_separate_output_root:
        return png_root
    return png_root.parent / f"{png_root.name}_staged_input"


def build_manifest_path(output_root: Path) -> Path:
    return output_root / ".cdmw_manifest.json"


def load_incremental_manifest(manifest_path: Path) -> Dict[str, Dict[str, object]]:
    source_path = manifest_path
    if not source_path.exists():
        legacy_paths = (
            manifest_path.with_name(".crimson_forge_toolkit_manifest.json"),
            manifest_path.with_name(".dds_rebuild_manifest.json"),
        )
        source_path = next((legacy_path for legacy_path in legacy_paths if legacy_path.exists()), source_path)
        if not source_path.exists():
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
        "color_args": list(output_settings.texconv_color_args),
        "extra_args": list(output_settings.texconv_extra_args),
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


def _read_loose_sidecar_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    if len(raw) > _LOOSE_SIDECAR_TEXT_LIMIT:
        raw = raw[:_LOOSE_SIDECAR_TEXT_LIMIT]
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            return raw.decode(encoding, errors="ignore")
        except Exception:
            continue
    return ""


def _build_loose_sidecar_index(
    root: Path,
) -> Tuple[
    Dict[str, List[Path]],
    Dict[str, List[Path]],
    Dict[str, List[Path]],
    Dict[str, List[Path]],
    Dict[Path, str],
]:
    by_group: Dict[str, List[Path]] = defaultdict(list)
    by_folder: Dict[str, List[Path]] = defaultdict(list)
    by_texture_path: Dict[str, List[Path]] = defaultdict(list)
    by_texture_basename: Dict[str, List[Path]] = defaultdict(list)
    text_cache: Dict[Path, str] = {}
    if not root.exists() or not root.is_dir():
        return {}, {}, {}, {}, {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _LOOSE_SEMANTIC_SIDECAR_EXTENSIONS:
            continue
        try:
            rel_text = path.relative_to(root).as_posix()
        except Exception:
            continue
        by_group[derive_texture_group_key(rel_text)].append(path)
        by_folder[str(path.relative_to(root).parent).replace("\\", "/")].append(path)
        text = _read_loose_sidecar_text(path)
        text_cache[path] = text
        if text and path.suffix.lower() in {".xml", ".pami"}:
            for binding in parse_texture_sidecar_bindings(text, sidecar_path=rel_text):
                normalized_texture = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
                if not normalized_texture:
                    continue
                by_texture_path[normalized_texture].append(path)
                texture_basename = PurePosixPath(normalized_texture).name
                if texture_basename:
                    by_texture_basename[texture_basename].append(path)
    return dict(by_group), dict(by_folder), dict(by_texture_path), dict(by_texture_basename), text_cache


def _collect_loose_sidecar_texts(
    root: Path,
    relative_path: Path,
    *,
    sidecars_by_group: Dict[str, List[Path]],
    sidecars_by_folder: Dict[str, List[Path]],
    sidecars_by_texture_path: Dict[str, List[Path]],
    sidecars_by_texture_basename: Dict[str, List[Path]],
    text_cache: Dict[Path, str],
    limit: int = 6,
) -> List[str]:
    rel_text = relative_path.as_posix()
    group_key = derive_texture_group_key(rel_text)
    folder_key = str(relative_path.parent).replace("\\", "/")
    normalized_target = normalize_texture_reference_for_sidecar_lookup(rel_text)
    target_basename = PurePosixPath(normalized_target).name
    candidates: List[Tuple[Path, bool]] = []
    seen: set[Path] = set()

    def add_candidate(path: Path, *, exact_match: bool) -> None:
        if path not in seen:
            seen.add(path)
            candidates.append((path, exact_match))

    for path in sidecars_by_texture_path.get(normalized_target, []):
        add_candidate(path, exact_match=True)
    for path in sidecars_by_texture_basename.get(target_basename, []):
        add_candidate(path, exact_match=True)
    for path in sidecars_by_group.get(group_key, []):
        add_candidate(path, exact_match=False)
    for path in sidecars_by_folder.get(folder_key, []):
        add_candidate(path, exact_match=False)

    snippets: List[str] = []
    target_name = relative_path.name.lower()
    target_stem = relative_path.stem.lower()
    for path, exact_match in candidates[:limit]:
        text = text_cache.get(path)
        if text is None:
            text = _read_loose_sidecar_text(path)
            text_cache[path] = text
        lowered = text.lower()
        if lowered and (
            exact_match
            or target_name in lowered
            or target_stem in lowered
            or derive_texture_group_key(path.relative_to(root).as_posix()).lower() == group_key.lower()
        ):
            snippets.append(text)
    return snippets


def _collect_texture_preview_sample(image_path: Path) -> Optional[TexturePreviewSample]:
    if PilImage is None:
        return None
    try:
        image_module = cast(Any, PilImage)
        with image_module.open(image_path) as image_handle:
            image = cast(Any, image_handle)
            working = image.convert("RGBA")
            resampling = getattr(getattr(image_module, "Resampling", image_module), "BICUBIC", getattr(image_module, "BICUBIC", 3))
            if max(working.size) > 64:
                working.thumbnail((64, 64), resampling)
            pixels = cast(List[Tuple[int, int, int, int]], list(working.getdata()))
    except Exception:
        return None

    if not pixels:
        return None

    sample_count = len(pixels)
    sum_r = sum_g = sum_b = sum_a = 0.0
    sum_luma = 0.0
    sum_chroma = 0.0
    min_luma = 255.0
    max_luma = 0.0
    opaque_count = 0
    transparent_count = 0

    for r, g, b, a in pixels:
        luma = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
        chroma = float(max(r, g, b) - min(r, g, b))
        sum_r += r
        sum_g += g
        sum_b += b
        sum_a += a
        sum_luma += luma
        sum_chroma += chroma
        min_luma = min(min_luma, luma)
        max_luma = max(max_luma, luma)
        if a >= 250:
            opaque_count += 1
        if a <= 5:
            transparent_count += 1

    return TexturePreviewSample(
        mean_r=sum_r / sample_count,
        mean_g=sum_g / sample_count,
        mean_b=sum_b / sample_count,
        mean_a=sum_a / sample_count,
        luma_mean=sum_luma / sample_count,
        luma_range=max_luma - min_luma,
        mean_chroma=sum_chroma / sample_count,
        opaque_fraction=opaque_count / sample_count,
        transparent_fraction=transparent_count / sample_count,
    )


def _preview_sample_for_unknown_dds(texconv_path: Path, dds_path: Path, texture_type: str) -> Optional[TexturePreviewSample]:
    if texture_type != "unknown":
        return None
    try:
        preview_path = ensure_dds_preview_png(texconv_path, dds_path)
    except Exception:
        return None
    return _collect_texture_preview_sample(preview_path)


def build_texture_processing_plan(
    normalized: NormalizedConfig,
    dds_files: Sequence[Path],
    *,
    backend_matrix: Optional[BackendCapabilityMatrix] = None,
) -> List[TextureProcessingPlan]:
    resolved_backend_matrix = backend_matrix or _build_backend_capability_matrix(normalized)
    (
        sidecars_by_group,
        sidecars_by_folder,
        sidecars_by_texture_path,
        sidecars_by_texture_basename,
        sidecar_text_cache,
    ) = _build_loose_sidecar_index(normalized.original_dds_root)
    family_members_by_group: Dict[str, List[str]] = defaultdict(list)
    for dds_path in dds_files:
        rel_text = dds_path.relative_to(normalized.original_dds_root).as_posix()
        family_members_by_group[derive_texture_group_key(rel_text)].append(rel_text)
    plan: List[TextureProcessingPlan] = []
    for dds_path in dds_files:
        rel_path = dds_path.relative_to(normalized.original_dds_root)
        rel_display = rel_path.as_posix()
        family_members = tuple(family_members_by_group.get(derive_texture_group_key(rel_display), ()))
        coarse_texture_type = classify_texture_type(rel_display)
        dds_info = parse_dds(dds_path)
        sidecar_texts = _collect_loose_sidecar_texts(
            normalized.original_dds_root,
            rel_path,
            sidecars_by_group=sidecars_by_group,
            sidecars_by_folder=sidecars_by_folder,
            sidecars_by_texture_path=sidecars_by_texture_path,
            sidecars_by_texture_basename=sidecars_by_texture_basename,
            text_cache=sidecar_text_cache,
        )
        preview_sample = _preview_sample_for_unknown_dds(normalized.texconv_path, dds_path, coarse_texture_type)
        decision = suggest_texture_upscale_decision(
            rel_display,
            preset=normalized.upscale_texture_preset,
            original_texconv_format=dds_info.texconv_format,
            has_alpha=dds_info.has_alpha,
            sidecar_texts=sidecar_texts,
            enable_automatic_rules=normalized.enable_automatic_texture_rules,
            family_members=family_members,
            preview_sample=preview_sample,
        )
        rule = find_matching_texture_rule(rel_path, normalized.texture_rules)
        plan.append(
            _build_texture_processing_plan_entry(
                normalized,
                dds_path,
                rel_path,
                dds_info,
                decision,
                rule,
                resolved_backend_matrix,
            )
        )
    return plan


def _summarize_policy_size(
    normalized: NormalizedConfig,
    entry: TextureProcessingPlan,
) -> str:
    dds_info = entry.dds_info
    if entry.action == "preserve_original":
        return f"{dds_info.width}x{dds_info.height} (unchanged)"
    if normalized.dds_size_mode == DDS_SIZE_MODE_ORIGINAL:
        return f"{dds_info.width}x{dds_info.height} (match original)"
    if normalized.dds_size_mode == DDS_SIZE_MODE_CUSTOM:
        return f"{normalized.dds_custom_width}x{normalized.dds_custom_height} (custom)"
    if normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN and entry.action == "upscale_then_rebuild":
        effective_scale = max(1, int(getattr(entry.effective_ncnn_settings, "scale", normalized.ncnn_scale) or normalized.ncnn_scale))
        estimated_width = max(1, dds_info.width * effective_scale)
        estimated_height = max(1, dds_info.height * effective_scale)
        return f"{estimated_width}x{estimated_height} (estimated {effective_scale}x direct backend PNG)"
    if entry.action == "rebuild_from_high_precision_png" and normalized.enable_dds_staging:
        return "staged high-precision PNG size (resolved from DDS-to-PNG conversion)"
    if entry.action == "rebuild_from_high_precision_png":
        return "existing high-precision PNG size (resolved from PNG root)"
    if normalized.upscale_backend == UPSCALE_BACKEND_NONE and normalized.enable_dds_staging:
        return "staged PNG size (resolved from DDS-to-PNG conversion)"
    return "final PNG size (resolved at rebuild time)"


def _summarize_policy_mips(
    normalized: NormalizedConfig,
    entry: TextureProcessingPlan,
) -> str:
    dds_info = entry.dds_info
    if entry.action == "preserve_original":
        return f"{dds_info.mip_count} (unchanged)"
    if normalized.dds_mip_mode == DDS_MIP_MODE_MATCH_ORIGINAL:
        return f"{dds_info.mip_count} (match original)"
    if normalized.dds_mip_mode == DDS_MIP_MODE_SINGLE:
        return "1 (single mip)"
    if normalized.dds_mip_mode == DDS_MIP_MODE_CUSTOM:
        return f"{normalized.dds_custom_mip_count} (custom)"
    return "full chain"


def summarize_texture_workflow_rule(rule: Optional[TextureRule]) -> str:
    if rule is None:
        return "(none)"
    match_mode = "Exact" if str(getattr(rule, "match_mode", "glob") or "glob").strip().lower() == "exact" else "Glob"
    return f"{match_mode}: {rule.pattern}"


def summarize_effective_dds_override(entry: TextureProcessingPlan) -> str:
    parts: List[str] = []
    if entry.effective_output_override.format_value:
        parts.append(f"fmt={entry.effective_output_override.format_value}")
    if entry.effective_output_override.size_value:
        parts.append(f"size={entry.effective_output_override.size_value}")
    if entry.effective_output_override.mip_value:
        parts.append(f"mips={entry.effective_output_override.mip_value}")
    return ", ".join(parts) if parts else "Inherit main DDS Output"


def summarize_effective_ncnn_settings(
    normalized: NormalizedConfig,
    entry: TextureProcessingPlan,
) -> str:
    if normalized.upscale_backend != UPSCALE_BACKEND_REALESRGAN_NCNN:
        return "Ignored unless direct NCNN is selected"
    settings = entry.effective_ncnn_settings
    parts = [
        settings.model_name or "(inherit)",
        f"{settings.scale}x",
        f"tile {settings.tile_size}",
    ]
    if settings.post_correction_mode:
        parts.append(settings.post_correction_mode)
    if settings.extra_args:
        parts.append("extra args")
    return " | ".join(parts)


def build_texture_policy_preview_payload(
    normalized: NormalizedConfig,
    dds_files: Sequence[Path],
    *,
    processing_plan: Sequence[TextureProcessingPlan] = (),
    backend_matrix: Optional[BackendCapabilityMatrix] = None,
) -> Dict[str, object]:
    resolved_backend_matrix = backend_matrix or _build_backend_capability_matrix(normalized)
    plan = list(processing_plan) if processing_plan else build_texture_processing_plan(
        normalized,
        dds_files,
        backend_matrix=resolved_backend_matrix,
    )
    rows: List[Dict[str, object]] = []
    action_counts: Dict[str, int] = defaultdict(int)
    semantic_counts: Dict[str, int] = defaultdict(int)
    direct_backend_supported = normalized.upscale_backend in {
        UPSCALE_BACKEND_REALESRGAN_NCNN,
    }
    for entry in plan:
        final_action = entry.action
        final_reason = entry.action_reason
        output_format = entry.dds_info.texconv_format
        detail_notes = list(entry.decision.notes)
        effective_correction_mode = (
            entry.effective_ncnn_settings.post_correction_mode
            if normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN
            else normalized.upscale_post_correction_mode
        )
        correction_plan = build_source_match_plan_for_decision(
            effective_correction_mode,
            entry.decision,
            direct_backend_supported=direct_backend_supported,
            planner_path_kind=entry.path_kind,
            planner_profile_key=entry.profile.key,
        )
        detail_notes.append(
            "post-correction: "
            f"{describe_post_upscale_correction_mode(effective_correction_mode)} -> "
            f"{correction_plan.correction_action} ({correction_plan.correction_reason})"
        )
        if entry.workflow_profile is not None:
            detail_notes.append(f"workflow profile: {entry.workflow_profile.label} ({entry.workflow_profile.profile_id})")
            detail_notes.append(f"workflow DDS override: {summarize_effective_dds_override(entry)}")
            detail_notes.append(f"workflow NCNN override: {summarize_effective_ncnn_settings(normalized, entry)}")
        if entry.matched_rule is not None:
            detail_notes.append(f"matched texture rule: {summarize_texture_workflow_rule(entry.matched_rule)}")
        if entry.preserve_reason:
            detail_notes.append(f"preserve reason: {entry.preserve_reason}")
        if entry.lossy_intermediate_warning:
            detail_notes.append(entry.lossy_intermediate_warning)
        if entry.action not in {"preserve_original", "skip_by_rule"}:
            output_settings = _resolve_plan_output_settings(
                normalized,
                entry,
                entry.dds_info.width,
                entry.dds_info.height,
                has_alpha=entry.dds_info.has_alpha,
            )
            output_format = output_settings.texconv_format
            detail_notes.extend(output_settings.notes)
        elif entry.action == "skip_by_rule":
            output_format = "-"
        action_counts[final_action] += 1
        semantic_counts[entry.decision.semantic_subtype] += 1
        rows.append(
            {
                "path": entry.relative_path.as_posix(),
                "texture_type": entry.decision.texture_type,
                "semantic_subtype": entry.decision.semantic_subtype,
                "semantic_confidence": entry.decision.semantic_confidence,
                "alpha_mode": entry.decision.alpha_mode,
                "alpha_policy": entry.alpha_policy,
                "packed_channels": list(entry.decision.packed_channels),
                "intermediate_policy": entry.decision.intermediate_policy,
                "path_kind": entry.path_kind,
                "path_description": describe_processing_path_kind(entry.path_kind),
                "profile_key": entry.profile.key,
                "profile_label": entry.profile.label,
                "workflow_profile_id": entry.workflow_profile.profile_id if entry.workflow_profile is not None else "",
                "workflow_profile_label": entry.workflow_profile.label if entry.workflow_profile is not None else "(none)",
                "matched_rule": summarize_texture_workflow_rule(entry.matched_rule),
                "effective_dds_override": summarize_effective_dds_override(entry),
                "effective_ncnn_summary": summarize_effective_ncnn_settings(normalized, entry),
                "effective_ncnn_model": entry.effective_ncnn_settings.model_name,
                "effective_ncnn_scale": entry.effective_ncnn_settings.scale,
                "effective_ncnn_tile": entry.effective_ncnn_settings.tile_size,
                "effective_ncnn_extra_args": entry.effective_ncnn_settings.extra_args,
                "effective_ncnn_correction": entry.effective_ncnn_settings.post_correction_mode,
                "backend_compatible": entry.backend_capability.compatible,
                "backend_execution_mode": entry.backend_capability.execution_mode,
                "backend_reason": entry.backend_capability.reason,
                "original_format": entry.dds_info.texconv_format,
                "planned_format": output_format,
                "size_policy": _summarize_policy_size(normalized, entry),
                "mip_policy": _summarize_policy_mips(normalized, entry),
                "action": final_action,
                "action_reason": final_reason,
                "requires_png_processing": entry.requires_png_processing,
                "preserve_reason": entry.preserve_reason,
                "correction_mode": describe_post_upscale_correction_mode(effective_correction_mode),
                "correction_eligibility": correction_plan.correction_eligibility,
                "correction_action": correction_plan.correction_action,
                "correction_reason": correction_plan.correction_reason,
                "source_evidence": list(entry.decision.source_evidence),
                "notes": detail_notes,
            }
        )

    return {
        "rows": rows,
        "summary": {
            "total_files": len(plan),
            "actions": dict(sorted(action_counts.items())),
            "semantic_subtypes": dict(sorted(semantic_counts.items())),
            "backend": normalized.upscale_backend,
            "backend_visible_path_mode": resolved_backend_matrix.decision_for("visible_color_png_path").execution_mode,
            "backend_visible_path_allowed": resolved_backend_matrix.decision_for("visible_color_png_path").compatible,
            "backend_high_precision_path_mode": resolved_backend_matrix.decision_for("technical_high_precision_path").execution_mode,
            "backend_high_precision_path_allowed": resolved_backend_matrix.decision_for("technical_high_precision_path").compatible,
            "backend_planner_notes": list(resolved_backend_matrix.planner_notes),
            "correction_mode": describe_post_upscale_correction_mode(normalized.upscale_post_correction_mode),
            "path_kinds": dict(sorted((key, sum(1 for entry in plan if entry.path_kind == key)) for key in {entry.path_kind for entry in plan})),
            "png_root": str(normalized.png_root),
            "texture_editor_png_root": str(normalized.texture_editor_png_root) if normalized.texture_editor_png_root else "",
            "output_root": str(normalized.output_root),
            "staging_root": str(normalized.dds_staging_root) if normalized.dds_staging_root else "",
        },
    }


def _scan_direct_high_precision_png_inputs(
    normalized: NormalizedConfig,
    processing_plan: Sequence[TextureProcessingPlan],
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[int, List[str], List[str]]:
    if normalized.enable_dds_staging:
        return 0, [], []
    planned_entries = [
        entry
        for entry in processing_plan
        if entry.path_kind == "technical_high_precision_path"
        and entry.action == "rebuild_from_high_precision_png"
    ]
    if not planned_entries:
        return 0, [], []

    relative_index, basename_index, _png_count = find_png_matches_across_roots(
        (normalized.png_root, normalized.texture_editor_png_root),
        stop_event=stop_event,
    )
    missing_examples: List[str] = []
    invalid_examples: List[str] = []

    for entry in planned_entries:
        raise_if_cancelled(stop_event, "Preflight scan cancelled by user.")
        png_path, match_note = resolve_png(
            entry.relative_path,
            relative_index,
            basename_index,
            normalized.allow_unique_basename_fallback,
        )
        rel_text = entry.relative_path.as_posix()
        if png_path is None:
            if len(missing_examples) < 5:
                missing_examples.append(f"{rel_text} ({match_note})")
            continue
        validation_message = _validate_high_precision_staged_png(png_path, entry)
        if validation_message is not None and len(invalid_examples) < 5:
            invalid_examples.append(f"{rel_text} ({validation_message})")

    return len(planned_entries), missing_examples, invalid_examples


def build_preflight_report_lines(
    normalized: NormalizedConfig,
    dds_files: Sequence[Path],
    *,
    processing_plan: Sequence[TextureProcessingPlan] = (),
    chain_analysis: Optional[ChainnerChainAnalysis] = None,
    backend_matrix: Optional[BackendCapabilityMatrix] = None,
    texture_rules: Sequence[TextureRule] = (),
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    total_dds_bytes = 0
    texture_type_counts: Dict[str, int] = defaultdict(int)
    semantic_subtype_counts: Dict[str, int] = defaultdict(int)
    action_counts: Dict[str, int] = defaultdict(int)
    path_kind_counts: Dict[str, int] = defaultdict(int)
    preserve_reason_counts: Dict[str, int] = defaultdict(int)
    high_risk_examples: List[str] = []
    high_precision_examples: List[str] = []
    high_precision_path_examples: List[str] = []
    blocked_high_precision_examples: List[str] = []
    high_precision_input_scan_total = 0
    missing_high_precision_input_examples: List[str] = []
    invalid_high_precision_input_examples: List[str] = []
    plan_by_rel: Dict[str, TextureProcessingPlan] = {
        entry.relative_path.as_posix(): entry for entry in processing_plan
    }
    policy_examples: List[str] = []
    for path in dds_files:
        try:
            total_dds_bytes += path.stat().st_size
        except OSError:
            continue
        rel_text = path.relative_to(normalized.original_dds_root).as_posix()
        plan_entry = plan_by_rel.get(rel_text)
        if plan_entry is not None:
            texture_type = plan_entry.decision.texture_type
            semantic_subtype = plan_entry.decision.semantic_subtype
            action_counts[plan_entry.action] += 1
            path_kind_counts[plan_entry.path_kind] += 1
            if plan_entry.preserve_reason:
                preserve_reason_counts[plan_entry.preserve_reason] += 1
            semantic_subtype_counts[semantic_subtype] += 1
            if plan_entry.path_kind == "technical_high_precision_path" and len(high_precision_path_examples) < 5:
                high_precision_path_examples.append(rel_text)
            if (
                plan_entry.path_kind == "technical_high_precision_path"
                and plan_entry.action == "preserve_original"
                and len(blocked_high_precision_examples) < 5
            ):
                blocked_high_precision_examples.append(f"{rel_text} ({plan_entry.action_reason})")
            if len(policy_examples) < 8:
                policy_examples.append(
                    f"{rel_text} -> {plan_entry.action} [{texture_type}/{semantic_subtype}] profile={plan_entry.profile.key} path={plan_entry.path_kind}"
                )
        else:
            texture_type = classify_texture_type(rel_text)
            semantic_subtype = texture_type
        texture_type_counts[texture_type] += 1
        if len(high_risk_examples) < 5 and texture_type in {"height", "vector"}:
            high_risk_examples.append(rel_text)
        if len(high_precision_examples) < 5:
            try:
                info = plan_entry.dds_info if plan_entry is not None else parse_dds(path)
            except Exception:
                info = None
            if info is not None and ("FLOAT" in info.texconv_format or "SNORM" in info.texconv_format):
                high_precision_examples.append(f"{rel_text} [{info.texconv_format}]")
    (
        high_precision_input_scan_total,
        missing_high_precision_input_examples,
        invalid_high_precision_input_examples,
    ) = _scan_direct_high_precision_png_inputs(
        normalized,
        processing_plan,
        stop_event=stop_event,
    )

    lines = [
        "Preflight report:",
        f"- DDS files matching filter: {len(dds_files)}",
        f"- Original DDS root: {normalized.original_dds_root}",
        f"- PNG root: {normalized.png_root}",
        f"- Texture Editor PNG root: {normalized.texture_editor_png_root or '(not configured)'}",
        f"- Output root: {normalized.output_root}",
        f"- Upscaling backend: {normalized.upscale_backend}",
        f"- DDS staging: {'enabled' if normalized.enable_dds_staging else 'disabled'}",
    ]

    if normalized.enable_dds_staging and normalized.dds_staging_root is not None:
        lines.append(f"- DDS staging root: {normalized.dds_staging_root}")
        if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER:
            lines.append(
                "Warning: DDS-to-PNG conversion is enabled before chaiNNer. "
                "PNG-input chains should read PNG files from the staging root or another matching PNG folder. "
                "DDS-direct chains can ignore the staged PNGs if that is intentional."
            )
        if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER and "${staging_png_root}" not in normalized.chainner_override_json:
            lines.append("- Warning: staging is enabled, but your chaiNNer overrides do not reference ${staging_png_root}.")
        if normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
            lines.append(
                "Warning: DDS-to-PNG conversion is enabled before Real-ESRGAN NCNN. "
                "The NCNN stage will read source PNGs from the staging root and write its output into PNG root."
            )
    elif normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER and "${staging_png_root}" in normalized.chainner_override_json:
        lines.append(
            "- Error: chaiNNer overrides reference ${staging_png_root}, but DDS staging is disabled. "
            "Enable 'Create source PNGs from DDS before processing' or remove that token."
        )

    lines.extend(
        [
            f"- Incremental resume: {'enabled' if normalized.enable_incremental_resume else 'disabled'}",
            f"- Texture rules loaded: {len(texture_rules)}",
            f"- Estimated source DDS data: {total_dds_bytes / (1024 * 1024):.1f} MiB",
        ]
    )
    if backend_matrix is not None:
        visible_capability = backend_matrix.decision_for("visible_color_png_path")
        technical_capability = backend_matrix.decision_for("technical_high_precision_path")
        lines.append(
            "- Planner backend matrix: "
            f"visible_color_png_path={'allow' if visible_capability.compatible else 'preserve'} "
            f"({visible_capability.execution_mode}), "
            f"technical_high_precision_path={'allow' if technical_capability.compatible else 'preserve'} "
            f"({technical_capability.execution_mode})"
        )
        if backend_matrix.planner_notes:
            for note in backend_matrix.planner_notes[:5]:
                lines.append(f"- Planner backend note: {note}")
    if texture_type_counts:
        type_summary = ", ".join(
            f"{texture_type}={count}"
            for texture_type, count in sorted(texture_type_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        lines.append(f"- Texture-type summary: {type_summary}")
    if semantic_subtype_counts:
        subtype_summary = ", ".join(
            f"{subtype}={count}"
            for subtype, count in sorted(semantic_subtype_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        lines.append(f"- Semantic subtype summary: {subtype_summary}")
    if action_counts:
        action_summary = ", ".join(
            f"{action}={count}"
            for action, count in sorted(action_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        lines.append(f"- Per-texture policy summary: {action_summary}")
    if path_kind_counts:
        path_summary = ", ".join(
            f"{path_kind}={count}"
            for path_kind, count in sorted(path_kind_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        lines.append(f"- Planner path summary: {path_summary}")
    if preserve_reason_counts:
        preserved_due_to_technical = sum(
            count for reason, count in preserve_reason_counts.items() if "technical preserve path" in reason or "profile" in reason
        )
        preserved_due_to_precision = sum(
            count for reason, count in preserve_reason_counts.items() if "precision" in reason or "float" in reason or "snorm" in reason
        )
        preserved_due_to_alpha = sum(
            count for reason, count in preserve_reason_counts.items() if "alpha" in reason or "premultiplied" in reason
        )
        rebuilt_visible = path_kind_counts.get("visible_color_png_path", 0)
        rebuilt_high_precision = path_kind_counts.get("technical_high_precision_path", 0)
        lines.append(
            "- Planner summary counts: "
            f"technical_preserve={preserved_due_to_technical}, "
            f"precision_preserve={preserved_due_to_precision}, "
            f"alpha_preserve={preserved_due_to_alpha}, "
            f"visible_color_path={rebuilt_visible}, "
            f"technical_high_precision_path={rebuilt_high_precision}"
        )
    if policy_examples:
        lines.append("- Policy examples:")
        for example in policy_examples[:6]:
            lines.append(f"  {example}")
    if high_risk_examples:
        lines.append(
            "- Warning: precision-sensitive technical maps were detected "
            f"({'; '.join(high_risk_examples[:3])}). Safer presets keep these out of the upscale path."
        )
    if high_precision_examples:
        lines.append(
            "- Warning: float/snorm DDS formats were detected "
            f"({'; '.join(high_precision_examples[:3])}). PNG intermediates can lose precision for these assets."
        )
    if high_precision_path_examples:
        lines.append(
            "- Technical high-precision path examples: "
            + "; ".join(high_precision_path_examples[:3])
        )
    if high_precision_input_scan_total:
        lines.append(
            "- Technical high-precision PNG input preflight: "
            f"checked {high_precision_input_scan_total} planned files in PNG root because DDS staging is disabled."
        )
    if missing_high_precision_input_examples:
        lines.append(
            "- Warning: some planned technical high-precision files have no matching PNG input and will preserve the original DDS: "
            + "; ".join(missing_high_precision_input_examples[:3])
        )
    if invalid_high_precision_input_examples:
        lines.append(
            "- Warning: some planned technical high-precision files matched invalid PNG inputs and will preserve the original DDS: "
            + "; ".join(invalid_high_precision_input_examples[:3])
        )
    if blocked_high_precision_examples:
        lines.append(
            "- Technical high-precision path blocked under current settings: "
            + "; ".join(blocked_high_precision_examples[:3])
        )

    try:
        usage = shutil.disk_usage(normalized.output_root if normalized.output_root.exists() else normalized.output_root.parent)
        lines.append(f"- Free disk space near output root: {usage.free / (1024 * 1024 * 1024):.1f} GiB")
    except OSError:
        lines.append("- Free disk space near output root: unavailable")

    if normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
        lines.append(f"- Real-ESRGAN NCNN executable: {normalized.ncnn_exe_path}")
        lines.append(f"- Real-ESRGAN NCNN model folder: {normalized.ncnn_model_dir}")
        lines.append(f"- Real-ESRGAN NCNN model: {normalized.ncnn_model_name}")
        lines.append(
            f"- Real-ESRGAN NCNN scale/tile/preset: {normalized.ncnn_scale}x / tile {normalized.ncnn_tile_size} / {normalized.upscale_texture_preset}"
        )
        if normalized.ncnn_extra_args:
            lines.append(f"- Real-ESRGAN NCNN extra args: {normalized.ncnn_extra_args}")
        lines.append(f"- Direct post-upscale correction: {normalized.upscale_post_correction_mode}")
    lines.append(
        f"- Automatic color/format rules: {'enabled' if normalized.enable_automatic_texture_rules else 'disabled'}"
    )
    lines.append(
        f"- Expert unsafe technical override: {'enabled' if normalized.enable_unsafe_technical_override else 'disabled'}"
    )
    lines.append(
        f"- Retry with smaller tile: {'enabled' if normalized.retry_smaller_tile_on_failure else 'disabled'}"
    )
    if normalized.enable_automatic_texture_rules:
        lines.append(
            "- Automatic rules now keep color-like textures sRGB-aware, prefer BC5 for normals, apply alpha-aware mip hints for cutout data, distinguish grayscale/packed technical maps more explicitly, and preserve original float/vector or packed-data DDS files when the PNG intermediate would be unsafe."
        )
    if normalized.enable_unsafe_technical_override:
        lines.append(
            "- Warning: expert unsafe technical override is enabled, so technical maps may be forced through the generic visible-color PNG/upscale path instead of being preserved."
        )
    if normalized.upscale_backend != UPSCALE_BACKEND_NONE:
        lines.append(
            "- Safe preset behavior: files excluded by the selected preset are copied through as original DDS files instead of being rebuilt from PNG."
        )
    lines.append(
        f"- Ready mod package export: {'enabled' if normalized.enable_mod_ready_loose_export else 'disabled'}"
    )
    if normalized.enable_mod_ready_loose_export and normalized.mod_ready_export_root is not None:
        package_root = resolve_mod_package_root(normalized.mod_ready_export_root, normalized.mod_ready_package_info)
        lines.append(f"- Mod package parent root: {normalized.mod_ready_export_root}")
        lines.append(f"- Mod package folder: {package_root.name}")
        lines.append(f"- Mod package output: {package_root}")
        lines.append(f"- .no_encrypt file: {'enabled' if normalized.mod_ready_create_no_encrypt_file else 'disabled'}")
    if chain_analysis and chain_analysis.warnings:
        lines.append("- chaiNNer preflight warnings:")
        for warning in chain_analysis.warnings[:5]:
            lines.append(f"  {warning}")

    return lines


def collect_relative_dds_paths(
    root: Path,
    stop_event: Optional[threading.Event] = None,
) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files: List[Path] = []
    for path in root.rglob("*"):
        raise_if_cancelled(stop_event, "DDS path scan cancelled by user.")
        if not path.is_file() or path.suffix.lower() != ".dds":
            continue
        files.append(path.relative_to(root))
    files.sort()
    return files


def collect_compare_relative_paths(
    original_root: Path,
    output_root: Path,
    stop_event: Optional[threading.Event] = None,
) -> List[Path]:
    combined = set(collect_relative_dds_paths(original_root, stop_event=stop_event))
    combined.update(collect_relative_dds_paths(output_root, stop_event=stop_event))
    return sorted(combined)


def build_preview_png_command(
    texconv_path: Path,
    dds_path: Path,
    output_dir: Path,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> List[str]:
    cmd = [
        str(texconv_path),
        "-nologo",
        "-y",
        "-f",
        "R8G8B8A8_UNORM",
        "-ft",
        "png",
        "-o",
        str(output_dir),
    ]
    if width is not None and height is not None and width > 0 and height > 0:
        cmd.extend(["-w", str(int(width)), "-h", str(int(height))])
    cmd.append(str(dds_path))
    return cmd


def _staging_png_format_for_plan(entry: TextureProcessingPlan) -> str:
    if entry.path_kind == "technical_high_precision_path":
        return "R16_UNORM"
    return "R8G8B8A8_UNORM"


def _validate_high_precision_staged_png(
    png_path: Path,
    plan_entry: TextureProcessingPlan,
) -> Optional[str]:
    if str(plan_entry.path_kind or "").strip().lower() != "technical_high_precision_path":
        return None
    try:
        _width, _height, bit_depth, color_type = read_png_header_info(png_path)
    except Exception as exc:
        return f"Could not validate high-precision staged PNG: {exc}"
    if bit_depth < 16:
        return f"Expected a 16-bit staged PNG for the technical high-precision path, but got {bit_depth}-bit {describe_png_color_type(color_type)}."
    if color_type not in {0, 4}:
        return f"Expected a grayscale staged PNG for the technical high-precision path, but got {describe_png_color_type(color_type)}."
    if str(plan_entry.alpha_policy or "").strip().lower() == "none" and color_type == 4:
        return "Technical high-precision path unexpectedly staged grayscale+alpha PNG data for an alpha-free scalar texture."
    return None


def build_staging_png_command(
    texconv_path: Path,
    dds_path: Path,
    output_dir: Path,
    entry: TextureProcessingPlan,
) -> List[str]:
    return [
        str(texconv_path),
        "-nologo",
        "-y",
        "-f",
        _staging_png_format_for_plan(entry),
        "-ft",
        "png",
        "-o",
        str(output_dir),
        str(dds_path),
    ]


def ensure_dds_preview_png(
    texconv_path: Path,
    dds_path: Path,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Path:
    stat = dds_path.stat()
    texconv_stat = texconv_path.stat()
    cache_key = hashlib.sha256(
        (
            f"{dds_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
            f"::{texconv_path.resolve()}::{texconv_stat.st_size}::{texconv_stat.st_mtime_ns}"
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / APP_NAME / "preview_cache" / cache_key
    preview_path = cache_dir / f"{dds_path.stem}.png"
    preview_lock = _get_preview_cache_lock(cache_key)

    with preview_lock:
        if preview_path.exists():
            try:
                if preview_path.stat().st_size > 0:
                    return preview_path
            except OSError:
                pass

        cache_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_preview_png_command(texconv_path, dds_path, cache_dir)
        return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
        if return_code != 0:
            detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
            raise ValueError(f"Could not generate preview for {dds_path.name}: {detail}")

        if preview_path.exists():
            try:
                if preview_path.stat().st_size > 0:
                    return preview_path
            except OSError:
                pass

        candidates: List[Path] = []
        for candidate in sorted(cache_dir.glob("*.png")):
            try:
                if candidate.stat().st_size > 0:
                    candidates.append(candidate)
            except OSError:
                continue
        if candidates:
            return candidates[0]

    raise ValueError(f"texconv did not produce a PNG preview for {dds_path.name}.")


def _preview_resize_dimensions(
    width: int,
    height: int,
    *,
    max_dimension: int,
) -> Optional[Tuple[int, int]]:
    width = int(width)
    height = int(height)
    max_dimension = int(max_dimension)
    if width <= 0 or height <= 0 or max_dimension <= 0:
        return None
    longest = max(width, height)
    if longest <= max_dimension:
        return None
    scale = float(max_dimension) / float(longest)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    return target_width, target_height


def ensure_dds_display_preview_png(
    texconv_path: Path,
    dds_path: Path,
    *,
    dds_info: Optional[DdsInfo] = None,
    max_dimension: int = _COMPARE_DISPLAY_PREVIEW_MAX_DIMENSION,
    stop_event: Optional[threading.Event] = None,
) -> Path:
    resolved_info: Optional[DdsInfo] = dds_info
    try:
        if resolved_info is None:
            resolved_info = parse_dds(dds_path)
    except Exception as exc:
        if dds_info is not None:
            raise
        resolved_info = None
    if resolved_info is None:
        return ensure_dds_preview_png(texconv_path, dds_path, stop_event=stop_event)
    resize_dims = _preview_resize_dimensions(
        resolved_info.width,
        resolved_info.height,
        max_dimension=max_dimension,
    )
    if resize_dims is None:
        return ensure_dds_preview_png(texconv_path, dds_path, stop_event=stop_event)

    stat = dds_path.stat()
    texconv_stat = texconv_path.stat()
    target_width, target_height = resize_dims
    cache_key = hashlib.sha256(
        (
            f"display::{dds_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
            f"::{texconv_path.resolve()}::{texconv_stat.st_size}::{texconv_stat.st_mtime_ns}"
            f"::{target_width}x{target_height}"
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / APP_NAME / "preview_cache_display" / cache_key
    preview_path = cache_dir / f"{dds_path.stem}.png"
    preview_lock = _get_preview_cache_lock(cache_key)

    with preview_lock:
        raise_if_cancelled(stop_event, f"Display preview generation cancelled for {dds_path.name}.")
        if preview_path.exists():
            try:
                if preview_path.stat().st_size > 0:
                    return preview_path
            except OSError:
                pass

        cache_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_preview_png_command(
            texconv_path,
            dds_path,
            cache_dir,
            width=target_width,
            height=target_height,
        )
        return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
        if return_code != 0:
            detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
            raise ValueError(f"Could not generate display preview for {dds_path.name}: {detail}")

        if preview_path.exists():
            try:
                if preview_path.stat().st_size > 0:
                    return preview_path
            except OSError:
                pass

        for candidate in sorted(cache_dir.glob("*.png")):
            try:
                if candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                continue

    raise ValueError(f"texconv did not produce a display PNG preview for {dds_path.name}.")


def stage_dds_to_pngs(
    config: NormalizedConfig,
    processing_plan: Sequence[TextureProcessingPlan],
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

    total = len(processing_plan)
    if on_phase:
        on_phase("DDS Staging", "Extracting DDS files to PNG...", False)
    if on_log:
        on_log(f"Phase 0/2: staging policy-selected DDS files to PNG in {stage_root}")
    if on_phase_progress:
        on_phase_progress(0, total, f"0 / {total} DDS staging files")

    for index, entry in enumerate(processing_plan, start=1):
        raise_if_cancelled(stop_event)
        dds_path = entry.dds_path
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

        cmd = build_staging_png_command(config.texconv_path, dds_path, target_dir, entry)
        if config.dry_run:
            if on_log:
                on_log(
                    f"[{index}/{total}] STAGE DRYRUN {relative_path.as_posix()} -> "
                    f"{_staging_png_format_for_plan(entry)} staging PNG"
                )
        else:
            return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
            if return_code != 0:
                detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
                raise ValueError(f"Could not stage {relative_path.as_posix()} to PNG: {detail}")
            if entry.path_kind == "technical_high_precision_path":
                validation_message = _validate_high_precision_staged_png(target_png, entry)
                if validation_message is not None and on_log:
                    on_log(
                        f"[{index}/{total}] STAGE WARNING {relative_path.as_posix()} -> {validation_message}"
                    )
            if on_log:
                on_log(
                    f"[{index}/{total}] STAGE {relative_path.as_posix()} -> "
                    f"{_staging_png_format_for_plan(entry)} staging PNG"
                )
        if on_phase_progress:
            on_phase_progress(index, total, f"{index} / {total} DDS staging files")


def build_compare_preview_pane_result(
    texconv_path: Optional[Path],
    dds_path: Optional[Path],
    missing_message: str,
    planner_summary: str = "",
    *,
    stop_event: Optional[threading.Event] = None,
) -> ComparePreviewPaneResult:
    if texconv_path is None:
        return ComparePreviewPaneResult(status="missing", message="Set texconv.exe to enable DDS previews.")

    if dds_path is None or not dds_path.exists():
        return ComparePreviewPaneResult(status="missing", message=missing_message)

    try:
        metadata_summary = ""
        dds_info: Optional[DdsInfo] = None
        try:
            dds_info = parse_dds(dds_path.resolve())
            metadata_summary = f"Format: {dds_info.texconv_format} | Size: {dds_info.width}x{dds_info.height} | Mips: {dds_info.mip_count}"
        except Exception:
            metadata_summary = "DDS metadata unavailable."
        if planner_summary.strip():
            metadata_summary = f"{metadata_summary} | {planner_summary.strip()}"
        preview_png = ensure_dds_display_preview_png(
            texconv_path.resolve(),
            dds_path.resolve(),
            dds_info=dds_info,
            stop_event=stop_event,
        )
        return ComparePreviewPaneResult(
            status="ok",
            title=dds_path.name,
            preview_png_path=str(preview_png),
            metadata_summary=metadata_summary,
        )
    except Exception as exc:
        return ComparePreviewPaneResult(status="error", message=str(exc))


def _resolve_workflow_profiles_and_rules_from_config(
    config: AppConfig,
) -> Tuple[str, Tuple[TextureWorkflowProfile, ...], Tuple[TextureRule, ...]]:
    raw_text = str(getattr(config, "texture_rules_text", "") or "")
    workflow_profiles = coerce_texture_workflow_profiles(getattr(config, "workflow_profiles", ()))
    texture_rules = coerce_texture_workflow_rules(getattr(config, "texture_rules", ()))
    if not workflow_profiles and not texture_rules and raw_text.strip():
        workflow_profiles, texture_rules = migrate_legacy_texture_rules_to_structured(raw_text)
    elif should_seed_default_texture_workflow_state(workflow_profiles, texture_rules):
        workflow_profiles = build_default_texture_workflow_profiles()
        texture_rules = build_default_texture_workflow_rules()
    workflow_profiles, texture_rules = upgrade_default_texture_workflow_state(workflow_profiles, texture_rules)
    return raw_text, workflow_profiles, texture_rules


def _validate_workflow_rule_profile_links(
    workflow_profiles: Sequence[TextureWorkflowProfile],
    texture_rules: Sequence[TextureRule],
) -> None:
    valid_ids = {profile.profile_id for profile in workflow_profiles}
    for rule in texture_rules:
        target_id = str(getattr(rule, "workflow_profile_id", "") or "").strip()
        if target_id and target_id not in valid_ids:
            raise ValueError(f"Texture workflow rule '{rule.pattern}' references unknown workflow profile id '{target_id}'.")


def normalize_config_for_planning(config: AppConfig) -> NormalizedConfig:
    upscale_backend = str(getattr(config, "upscale_backend", "") or "").strip().lower()
    if upscale_backend not in {
        UPSCALE_BACKEND_NONE,
        UPSCALE_BACKEND_CHAINNER,
        UPSCALE_BACKEND_REALESRGAN_NCNN,
    }:
        upscale_backend = UPSCALE_BACKEND_CHAINNER if config.enable_chainner else UPSCALE_BACKEND_NONE
    texture_rules_text, workflow_profiles, texture_rules = _resolve_workflow_profiles_and_rules_from_config(config)
    _validate_workflow_rule_profile_links(workflow_profiles, texture_rules)

    original_dds_root = ensure_existing_dir(
        normalize_required_path(config.original_dds_root, "Original DDS root"),
        "Original DDS root",
    )
    png_root = normalize_required_path(config.png_root, "PNG root")
    texture_editor_png_root = normalize_optional_path(getattr(config, "texture_editor_png_root", ""))
    output_root = normalize_required_path(config.output_root, "Output root")
    dds_staging_root = normalize_optional_path(config.dds_staging_root)
    texconv_path = normalize_optional_path(config.texconv_path) or Path(str(config.texconv_path).strip() or ".").expanduser().resolve()
    csv_log_path = normalize_optional_path(config.csv_log_path) if config.csv_log_enabled else None
    chainner_exe_path = normalize_optional_path(config.chainner_exe_path)
    chainner_chain_path = normalize_optional_path(config.chainner_chain_path)
    ncnn_exe_path = normalize_optional_path(config.ncnn_exe_path)
    explicit_model_dir = normalize_optional_path(config.ncnn_model_dir)
    ncnn_model_dir = resolve_ncnn_model_dir(ncnn_exe_path, explicit_model_dir) or explicit_model_dir
    mod_ready_export_root = normalize_optional_path(config.mod_ready_export_root)
    mod_ready_package_info = ModPackageInfo(
        title=str(getattr(config, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE) or "").strip() or MOD_READY_PACKAGE_TITLE,
        version=str(getattr(config, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION) or "").strip() or MOD_READY_PACKAGE_VERSION,
        author=str(getattr(config, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR) or "").strip(),
        description=str(getattr(config, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION) or "").strip(),
        nexus_url=str(getattr(config, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL) or "").strip(),
    )
    mod_ready_export_options = build_mod_package_export_options_from_config(config)

    return NormalizedConfig(
        original_dds_root=original_dds_root,
        png_root=png_root,
        texture_editor_png_root=texture_editor_png_root,
        output_root=output_root,
        dds_staging_root=dds_staging_root,
        texconv_path=texconv_path,
        dds_format_mode=str(config.dds_format_mode or DEFAULT_DDS_FORMAT_MODE).strip().lower() or DEFAULT_DDS_FORMAT_MODE,
        dds_custom_format=str(config.dds_custom_format or DEFAULT_DDS_CUSTOM_FORMAT).strip() or DEFAULT_DDS_CUSTOM_FORMAT,
        dds_size_mode=str(config.dds_size_mode or DEFAULT_DDS_SIZE_MODE).strip().lower() or DEFAULT_DDS_SIZE_MODE,
        dds_custom_width=int(config.dds_custom_width),
        dds_custom_height=int(config.dds_custom_height),
        dds_mip_mode=str(config.dds_mip_mode or DEFAULT_DDS_MIP_MODE).strip().lower() or DEFAULT_DDS_MIP_MODE,
        dds_custom_mip_count=int(config.dds_custom_mip_count),
        enable_dds_staging=bool(config.enable_dds_staging),
        enable_incremental_resume=bool(config.enable_incremental_resume),
        texture_rules_text=texture_rules_text,
        texture_rules=texture_rules,
        workflow_profiles=workflow_profiles,
        dry_run=bool(config.dry_run),
        csv_log_path=csv_log_path,
        allow_unique_basename_fallback=bool(config.allow_unique_basename_fallback),
        overwrite_existing_dds=bool(config.overwrite_existing_dds),
        include_filter_patterns=parse_filter_patterns(str(config.include_filters or "")),
        upscale_backend=upscale_backend,
        enable_chainner=upscale_backend == UPSCALE_BACKEND_CHAINNER,
        chainner_exe_path=chainner_exe_path,
        chainner_chain_path=chainner_chain_path,
        chainner_override_json=str(config.chainner_override_json or ""),
        ncnn_exe_path=ncnn_exe_path,
        ncnn_model_dir=ncnn_model_dir,
        ncnn_model_name=str(config.ncnn_model_name or "").strip(),
        ncnn_scale=int(getattr(config, "ncnn_scale", REALESRGAN_NCNN_SCALE)),
        ncnn_tile_size=int(getattr(config, "ncnn_tile_size", REALESRGAN_NCNN_TILE_SIZE)),
        ncnn_extra_args=str(getattr(config, "ncnn_extra_args", REALESRGAN_NCNN_EXTRA_ARGS) or "").strip(),
        upscale_post_correction_mode=str(getattr(config, "upscale_post_correction_mode", DEFAULT_UPSCALE_POST_CORRECTION) or "").strip().lower() or DEFAULT_UPSCALE_POST_CORRECTION,
        upscale_texture_preset=str(getattr(config, "upscale_texture_preset", DEFAULT_UPSCALE_TEXTURE_PRESET) or "").strip().lower() or DEFAULT_UPSCALE_TEXTURE_PRESET,
        enable_automatic_texture_rules=bool(getattr(config, "enable_automatic_texture_rules", ENABLE_AUTOMATIC_TEXTURE_RULES)),
        enable_unsafe_technical_override=bool(getattr(config, "enable_unsafe_technical_override", ENABLE_UNSAFE_TECHNICAL_OVERRIDE)),
        retry_smaller_tile_on_failure=bool(getattr(config, "retry_smaller_tile_on_failure", RETRY_SMALLER_TILE_ON_FAILURE)),
        enable_mod_ready_loose_export=bool(getattr(config, "enable_mod_ready_loose_export", ENABLE_MOD_READY_LOOSE_EXPORT)),
        mod_ready_export_root=mod_ready_export_root,
        mod_ready_create_no_encrypt_file=bool(getattr(config, "mod_ready_create_no_encrypt_file", MOD_READY_CREATE_NO_ENCRYPT)),
        mod_ready_package_info=mod_ready_package_info,
        mod_ready_export_options=mod_ready_export_options,
    )


def normalize_config(config: AppConfig, *, validate_backend_runtime: bool = True) -> NormalizedConfig:
    upscale_backend = str(getattr(config, "upscale_backend", "") or "").strip().lower()
    if upscale_backend not in {
        UPSCALE_BACKEND_NONE,
        UPSCALE_BACKEND_CHAINNER,
        UPSCALE_BACKEND_REALESRGAN_NCNN,
    }:
        upscale_backend = UPSCALE_BACKEND_CHAINNER if config.enable_chainner else UPSCALE_BACKEND_NONE
    use_chainner = upscale_backend == UPSCALE_BACKEND_CHAINNER
    use_ncnn = upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN
    texture_rules_text, workflow_profiles, texture_rules = _resolve_workflow_profiles_and_rules_from_config(config)
    _validate_workflow_rule_profile_links(workflow_profiles, texture_rules)

    original_dds_root = ensure_existing_dir(
        normalize_required_path(config.original_dds_root, "Original DDS root"),
        "Original DDS root",
    )
    png_root = normalize_required_path(config.png_root, "PNG root")
    texture_editor_png_root = normalize_optional_path(getattr(config, "texture_editor_png_root", ""))
    if not config.enable_dds_staging and (
        upscale_backend == UPSCALE_BACKEND_NONE
        or (validate_backend_runtime and upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN)
    ):
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
    if use_chainner:
        if validate_backend_runtime:
            chainner_exe_path = ensure_existing_file(
                normalize_required_path(config.chainner_exe_path, "chaiNNer executable path"),
                "chaiNNer executable path",
            )
            chainner_chain_path = ensure_existing_file(
                normalize_required_path(config.chainner_chain_path, "chaiNNer chain path"),
                "chaiNNer chain path",
            )
        else:
            chainner_exe_path = normalize_optional_path(config.chainner_exe_path)
            chainner_chain_path = normalize_optional_path(config.chainner_chain_path)

    ncnn_exe_path: Optional[Path] = None
    ncnn_model_dir: Optional[Path] = None
    ncnn_model_name = ""
    ncnn_scale = int(getattr(config, "ncnn_scale", REALESRGAN_NCNN_SCALE))
    ncnn_tile_size = int(getattr(config, "ncnn_tile_size", REALESRGAN_NCNN_TILE_SIZE))
    ncnn_extra_args = str(getattr(config, "ncnn_extra_args", REALESRGAN_NCNN_EXTRA_ARGS) or "").strip()
    upscale_post_correction_mode = str(
        getattr(config, "upscale_post_correction_mode", DEFAULT_UPSCALE_POST_CORRECTION) or ""
    ).strip().lower() or DEFAULT_UPSCALE_POST_CORRECTION
    upscale_texture_preset = str(getattr(config, "upscale_texture_preset", DEFAULT_UPSCALE_TEXTURE_PRESET) or "").strip().lower() or DEFAULT_UPSCALE_TEXTURE_PRESET
    upscale_post_correction_mode = _validate_choice(
        upscale_post_correction_mode,
        (
            UPSCALE_POST_CORRECTION_NONE,
            UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
            UPSCALE_POST_CORRECTION_MATCH_LEVELS,
            UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
        ),
        "post-upscale correction mode",
    )
    if use_ncnn:
        explicit_model_dir = normalize_optional_path(config.ncnn_model_dir)
        if validate_backend_runtime:
            ncnn_exe_path = ensure_existing_file(
                normalize_required_path(config.ncnn_exe_path, "Real-ESRGAN NCNN executable path"),
                "Real-ESRGAN NCNN executable path",
            )
            resolved_model_dir = resolve_ncnn_model_dir(ncnn_exe_path, explicit_model_dir)
            if resolved_model_dir is None:
                raise ValueError(
                    "Real-ESRGAN NCNN model folder is not set and no default 'models' folder was found beside the executable."
                )
            ncnn_model_dir = ensure_existing_dir(resolved_model_dir, "Real-ESRGAN NCNN model folder")
            discovered_models = discover_realesrgan_ncnn_models(ncnn_exe_path, ncnn_model_dir)
            if not discovered_models:
                raise ValueError(f"No Real-ESRGAN NCNN models (.param + .bin) were found in {ncnn_model_dir}.")
            available_model_names = {name for name, _ in discovered_models}
            ncnn_model_name = config.ncnn_model_name.strip() or next(iter(sorted(available_model_names)))
            if ncnn_model_name not in available_model_names:
                raise ValueError(
                    f"Real-ESRGAN NCNN model '{ncnn_model_name}' was not found in {ncnn_model_dir}."
                )
            for workflow_profile in workflow_profiles:
                if workflow_profile.ncnn_model_name and workflow_profile.ncnn_model_name not in available_model_names:
                    raise ValueError(
                        f"Workflow profile '{workflow_profile.label}' references missing Real-ESRGAN NCNN model '{workflow_profile.ncnn_model_name}'."
                    )
        else:
            ncnn_exe_path = normalize_optional_path(config.ncnn_exe_path)
            ncnn_model_dir = resolve_ncnn_model_dir(ncnn_exe_path, explicit_model_dir) or explicit_model_dir
            ncnn_model_name = config.ncnn_model_name.strip()
        if ncnn_scale not in {2, 3, 4}:
            raise ValueError("Real-ESRGAN NCNN scale must be 2, 3, or 4.")
        if ncnn_tile_size < 0:
            raise ValueError("Real-ESRGAN NCNN tile size must be 0 or greater.")
        if upscale_texture_preset not in {
            UPSCALE_TEXTURE_PRESET_BALANCED,
            UPSCALE_TEXTURE_PRESET_COLOR_UI,
            UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
            UPSCALE_TEXTURE_PRESET_ALL,
        }:
            raise ValueError(f"Unknown upscale texture preset: {upscale_texture_preset}")

    enable_automatic_texture_rules = bool(getattr(config, "enable_automatic_texture_rules", ENABLE_AUTOMATIC_TEXTURE_RULES))
    enable_unsafe_technical_override = bool(getattr(config, "enable_unsafe_technical_override", ENABLE_UNSAFE_TECHNICAL_OVERRIDE))
    retry_smaller_tile_on_failure = bool(getattr(config, "retry_smaller_tile_on_failure", RETRY_SMALLER_TILE_ON_FAILURE))
    enable_mod_ready_loose_export = bool(getattr(config, "enable_mod_ready_loose_export", ENABLE_MOD_READY_LOOSE_EXPORT))
    mod_ready_export_root: Optional[Path] = None
    if enable_mod_ready_loose_export:
        explicit_mod_ready_export_root = normalize_optional_path(getattr(config, "mod_ready_export_root", ""))
        mod_ready_export_root = explicit_mod_ready_export_root or resolve_default_mod_ready_export_root(output_root)
        if mod_ready_export_root.resolve() == output_root.resolve():
            raise ValueError("Mod-ready export root must be different from the main output root.")
    mod_ready_package_info = ModPackageInfo(
        title=str(getattr(config, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE) or "").strip() or MOD_READY_PACKAGE_TITLE,
        version=str(getattr(config, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION) or "").strip() or MOD_READY_PACKAGE_VERSION,
        author=str(getattr(config, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR) or "").strip(),
        description=str(getattr(config, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION) or "").strip(),
        nexus_url=str(getattr(config, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL) or "").strip(),
    )
    mod_ready_export_options = build_mod_package_export_options_from_config(config)

    dds_staging_root: Optional[Path] = None
    if config.enable_dds_staging:
        if config.dds_staging_root.strip():
            dds_staging_root = normalize_required_path(config.dds_staging_root, "DDS staging root")
        else:
            dds_staging_root = resolve_default_staging_png_root(png_root, use_chainner or use_ncnn).resolve()
        if validate_backend_runtime and (use_chainner or use_ncnn) and dds_staging_root.resolve() == png_root.resolve():
            raise ValueError("DDS staging root must be different from the final PNG root when an upscaling backend is enabled.")

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

    return NormalizedConfig(
        original_dds_root=original_dds_root,
        png_root=png_root,
        texture_editor_png_root=texture_editor_png_root,
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
        texture_rules_text=texture_rules_text,
        dry_run=config.dry_run,
        csv_log_path=csv_log_path,
        allow_unique_basename_fallback=config.allow_unique_basename_fallback,
        overwrite_existing_dds=config.overwrite_existing_dds,
        include_filter_patterns=parse_filter_patterns(config.include_filters),
        upscale_backend=upscale_backend,
        enable_chainner=use_chainner,
        chainner_exe_path=chainner_exe_path,
        chainner_chain_path=chainner_chain_path,
        chainner_override_json=config.chainner_override_json,
        ncnn_exe_path=ncnn_exe_path,
        ncnn_model_dir=ncnn_model_dir,
        ncnn_model_name=ncnn_model_name,
        ncnn_scale=ncnn_scale,
        ncnn_tile_size=ncnn_tile_size,
        ncnn_extra_args=ncnn_extra_args,
        upscale_post_correction_mode=upscale_post_correction_mode,
        upscale_texture_preset=upscale_texture_preset,
        enable_automatic_texture_rules=enable_automatic_texture_rules,
        enable_unsafe_technical_override=enable_unsafe_technical_override,
        retry_smaller_tile_on_failure=retry_smaller_tile_on_failure,
        enable_mod_ready_loose_export=enable_mod_ready_loose_export,
        mod_ready_export_root=mod_ready_export_root,
        mod_ready_create_no_encrypt_file=bool(getattr(config, "mod_ready_create_no_encrypt_file", MOD_READY_CREATE_NO_ENCRYPT)),
        mod_ready_package_info=mod_ready_package_info,
        mod_ready_export_options=mod_ready_export_options,
        texture_rules=texture_rules,
        workflow_profiles=workflow_profiles,
    )


def validate_backend_runtime_requirements(normalized: NormalizedConfig) -> NormalizedConfig:
    backend = normalized.upscale_backend
    if backend == UPSCALE_BACKEND_NONE:
        return normalized

    if normalized.enable_dds_staging and normalized.dds_staging_root is not None:
        if normalized.dds_staging_root.resolve() == normalized.png_root.resolve():
            raise ValueError("DDS staging root must be different from the final PNG root when an upscaling backend is enabled.")

    if backend == UPSCALE_BACKEND_CHAINNER:
        normalized.chainner_exe_path = require_existing_file(normalized.chainner_exe_path, "chaiNNer executable path")
        normalized.chainner_chain_path = require_existing_file(normalized.chainner_chain_path, "chaiNNer chain path")
        return normalized

    if backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
        normalized.ncnn_exe_path = require_existing_file(normalized.ncnn_exe_path, "Real-ESRGAN NCNN executable path")
        if not normalized.enable_dds_staging:
            ensure_existing_dir(normalized.png_root, "PNG root")
        resolved_model_dir = resolve_ncnn_model_dir(normalized.ncnn_exe_path, normalized.ncnn_model_dir)
        if resolved_model_dir is None:
            raise ValueError(
                "Real-ESRGAN NCNN model folder is not set and no default 'models' folder was found beside the executable."
            )
        normalized.ncnn_model_dir = ensure_existing_dir(resolved_model_dir, "Real-ESRGAN NCNN model folder")
        discovered_models = discover_realesrgan_ncnn_models(normalized.ncnn_exe_path, normalized.ncnn_model_dir)
        if not discovered_models:
            raise ValueError(f"No Real-ESRGAN NCNN models (.param + .bin) were found in {normalized.ncnn_model_dir}.")
        available_model_names = {name for name, _ in discovered_models}
        normalized.ncnn_model_name = normalized.ncnn_model_name.strip() or next(iter(sorted(available_model_names)))
        if normalized.ncnn_model_name not in available_model_names:
            raise ValueError(
                f"Real-ESRGAN NCNN model '{normalized.ncnn_model_name}' was not found in {normalized.ncnn_model_dir}."
            )
        return normalized

    return normalized


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


def overlay_texture_editor_pngs(
    texture_editor_png_root: Optional[Path],
    target_root: Path,
    relative_paths: Sequence[Path],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> int:
    if texture_editor_png_root is None:
        return 0
    if not texture_editor_png_root.exists() or not texture_editor_png_root.is_dir():
        return 0
    if texture_editor_png_root.resolve() == target_root.resolve():
        return 0

    copied = 0
    for relative_path in relative_paths:
        raise_if_cancelled(stop_event)
        relative_png = Path(PurePosixPath(relative_path).with_suffix(".png").as_posix())
        source_png = texture_editor_png_root / relative_png
        if not source_png.exists() or not source_png.is_file():
            continue
        destination_png = target_root / relative_png
        destination_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_png, destination_png)
        copied += 1
        if on_log is not None:
            on_log(
                f"Applied Texture Editor PNG override: {source_png.name} -> {destination_png.relative_to(target_root).as_posix()}"
            )

    if copied > 0 and on_log is not None:
        on_log(f"Applied {copied} Texture Editor PNG override(s) into {target_root}.")
    return copied


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
    normalized = normalize_config(config, validate_backend_runtime=False)
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
        f"upscale_backend={normalized.upscale_backend}, "
        f"dds_staging={'enabled' if normalized.enable_dds_staging else 'disabled'}, "
        f"incremental_resume={'enabled' if normalized.enable_incremental_resume else 'disabled'}, "
        f"dry_run={'on' if normalized.dry_run else 'off'}, "
        f"dds_format_mode={normalized.dds_format_mode}, "
        f"dds_size_mode={normalized.dds_size_mode}, "
        f"dds_mip_mode={normalized.dds_mip_mode}, "
        f"overwrite_existing_dds={'on' if normalized.overwrite_existing_dds else 'off'}."
    )
    if normalized.enable_unsafe_technical_override:
        emit_log(
            "Expert unsafe technical override is enabled. Technical maps may be forced through the generic visible-color PNG/upscale path instead of being preserved."
        )
    if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER:
        emit_log(f"chaiNNer executable: {normalized.chainner_exe_path}")
        emit_log(f"chaiNNer chain: {normalized.chainner_chain_path}")
    elif normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
        emit_log(f"Real-ESRGAN NCNN executable: {normalized.ncnn_exe_path}")
        emit_log(f"Real-ESRGAN NCNN model folder: {normalized.ncnn_model_dir}")
        emit_log(f"Real-ESRGAN NCNN model: {normalized.ncnn_model_name}")
        emit_log(
            f"Real-ESRGAN NCNN scale/tile/preset: {normalized.ncnn_scale}x / tile {normalized.ncnn_tile_size} / {normalized.upscale_texture_preset}"
        )
        emit_log(f"Direct post-upscale correction: {normalized.upscale_post_correction_mode}")
    else:
        emit_log("Upscaling stage is disabled, so the app will rebuild DDS from the existing PNG root.")
    emit_log(
        f"Automatic texture rules={'enabled' if normalized.enable_automatic_texture_rules else 'disabled'}, "
        f"retry_smaller_tile={'enabled' if normalized.retry_smaller_tile_on_failure else 'disabled'}, "
        f"ready_mod_package={'enabled' if normalized.enable_mod_ready_loose_export else 'disabled'}."
    )
    if normalized.enable_mod_ready_loose_export and normalized.mod_ready_export_root is not None:
        package_root = resolve_mod_package_root(normalized.mod_ready_export_root, normalized.mod_ready_package_info)
        emit_log(f"Mod package parent root: {normalized.mod_ready_export_root}")
        emit_log(f"Mod package folder: {package_root.name}")
        emit_log(f"Create .no_encrypt file: {'yes' if normalized.mod_ready_create_no_encrypt_file else 'no'}")
    if normalized.texture_editor_png_root is not None:
        emit_log(
            f"Texture Editor PNG override root: {normalized.texture_editor_png_root} "
            "(matching relative PNGs here take precedence over PNG root)."
        )
    if normalized.enable_dds_staging:
        if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER:
            emit_log(
                f"File flow: Original DDS -> Staging PNG root ({normalized.dds_staging_root}) -> chaiNNer -> PNG root ({normalized.png_root}) -> DDS rebuild -> Output root ({normalized.output_root})"
            )
        elif normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
            emit_log(
                f"File flow: Original DDS -> Staging PNG root ({normalized.dds_staging_root}) -> Real-ESRGAN NCNN -> PNG root ({normalized.png_root}) -> DDS rebuild -> Output root ({normalized.output_root})"
            )
        else:
            emit_log(
                f"File flow: Original DDS -> PNG root ({normalized.png_root}). With no backend selected, processing stops after PNG conversion."
            )
    else:
        if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER:
            emit_log(
                f"File flow: Existing PNG root ({normalized.png_root}) -> chaiNNer -> PNG root ({normalized.png_root}) -> DDS rebuild -> Output root ({normalized.output_root})"
            )
        elif normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
            emit_log(
                f"File flow: Existing PNG root ({normalized.png_root}) -> Real-ESRGAN NCNN -> PNG root ({normalized.png_root}) -> DDS rebuild -> Output root ({normalized.output_root})"
            )
        else:
            emit_log(
                f"File flow: Existing PNG root ({normalized.png_root}) -> DDS rebuild -> Output root ({normalized.output_root})"
            )

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

    backend_matrix = _build_backend_capability_matrix(normalized)
    processing_plan = build_texture_processing_plan(normalized, dds_files, backend_matrix=backend_matrix)
    plan_by_rel = {entry.relative_path.as_posix(): entry for entry in processing_plan}
    plan_entries_requiring_png = [entry for entry in processing_plan if entry.requires_png_processing]
    dds_files_requiring_png = [entry.dds_path for entry in plan_entries_requiring_png]

    if normalized.upscale_backend == UPSCALE_BACKEND_NONE or dds_files_requiring_png:
        normalized = validate_backend_runtime_requirements(normalized)
    elif normalized.upscale_backend != UPSCALE_BACKEND_NONE:
        emit_log(
            "Backend/runtime validation was skipped because the current preset and automatic rules kept every matched DDS out of the PNG/upscale path."
        )

    chain_analysis = (
        analyze_chainner_chain(normalized.chainner_chain_path, normalized)
        if normalized.enable_chainner and normalized.chainner_chain_path and dds_files_requiring_png
        else None
    )
    if chain_analysis is not None:
        backend_matrix = _build_backend_capability_matrix(normalized, chain_analysis=chain_analysis)
        processing_plan = build_texture_processing_plan(normalized, dds_files, backend_matrix=backend_matrix)
        plan_by_rel = {entry.relative_path.as_posix(): entry for entry in processing_plan}
        plan_entries_requiring_png = [entry for entry in processing_plan if entry.requires_png_processing]
        dds_files_requiring_png = [entry.dds_path for entry in plan_entries_requiring_png]
    for line in build_preflight_report_lines(
        normalized,
        dds_files,
        processing_plan=processing_plan,
        chain_analysis=chain_analysis,
        backend_matrix=backend_matrix,
        texture_rules=normalized.texture_rules,
        stop_event=stop_event,
    ):
        emit_log(line)

    if normalized.enable_dds_staging and dds_files_requiring_png:
        stage_dds_to_pngs(
            normalized,
            plan_entries_requiring_png,
            on_log=on_log,
            on_phase=on_phase,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )
        if normalized.upscale_backend == UPSCALE_BACKEND_NONE and normalized.dds_staging_root is not None:
            active_png_root = normalized.dds_staging_root
    elif normalized.enable_dds_staging:
        emit_log("DDS staging skipped because no files require PNG/upscale processing under the current policy.")

    if dds_files_requiring_png:
        overlay_texture_editor_pngs(
            normalized.texture_editor_png_root,
            active_png_root,
            [entry.relative_path for entry in plan_entries_requiring_png],
            on_log=on_log,
            stop_event=stop_event,
        )

    if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER and dds_files_requiring_png:
        run_chainner_stage(
            normalized,
            input_root=active_png_root,
            expected_relative_paths=[entry.relative_path.with_suffix(".png") for entry in plan_entries_requiring_png],
            expected_output_total=len(dds_files_requiring_png),
            on_log=on_log,
            on_phase=on_phase,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )
    elif normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN and dds_files_requiring_png:
        run_realesrgan_ncnn_stage(
            normalized,
            processing_plan=processing_plan,
            on_log=on_log,
            on_phase=on_phase,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )
    elif normalized.upscale_backend != UPSCALE_BACKEND_NONE:
        emit_log("No files require direct PNG/upscale processing under the current preset and automatic rules. The selected backend will be skipped.")

    relative_png_index: Dict[str, Path] = {}
    basename_png_index: Dict[str, List[Path]] = {}
    png_count = 0
    if dds_files_requiring_png:
        emit_phase("DDS Rebuild", "Indexing PNG files...", False)
        emit_phase_progress(0, 0, "Indexing PNG files...")
        emit_log("Indexing PNG files...")
        relative_png_index, basename_png_index, png_count = find_png_matches_across_roots(
            (
                active_png_root,
                normalized.texture_editor_png_root
                if normalized.texture_editor_png_root is not None
                and normalized.texture_editor_png_root.resolve() != active_png_root.resolve()
                else None,
            ),
            stop_event=stop_event,
        )
        emit_log(f"Indexed {png_count} PNG files.")
        if normalized.upscale_backend == UPSCALE_BACKEND_CHAINNER and png_count == 0 and dds_files_requiring_png:
            chain_analysis = chain_analysis or ChainnerChainAnalysis()
            detail = ""
            if chain_analysis.warnings:
                detail = " " + " | ".join(chain_analysis.warnings[:3])
            raise ValueError(
                "chaiNNer finished, but no PNG files were found in the configured PNG root. "
                "The chain likely still points at old folders or writes somewhere else."
                + detail
            )
        if normalized.upscale_backend == UPSCALE_BACKEND_REALESRGAN_NCNN and png_count == 0 and dds_files_requiring_png:
            raise ValueError(
                "Real-ESRGAN NCNN finished, but no PNG files were found in the configured PNG root. "
                "Verify the NCNN executable, model folder, and selected model."
            )
    else:
        emit_log("No policy-selected files require PNG matching. DDS rebuild will use preserve-original copy-through actions only.")
    emit_phase_progress(0, total, f"0 / {total} DDS files")
    emit_log(
        f"Found {total} DDS files to process. "
        f"{len(dds_files_requiring_png)} file(s) require PNG/upscale processing under the current policy."
    )
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

            plan_entry = plan_by_rel.get(rel_display)
            if plan_entry is None:
                raise RuntimeError(f"Missing planner entry for DDS rebuild: {rel_display}")
            dds_info = plan_entry.dds_info
            decision = plan_entry.decision
            if plan_entry.action in {"preserve_original", "skip_by_rule"}:
                if plan_entry.action == "skip_by_rule":
                    skipped += 1
                    note = plan_entry.action_reason
                    results.append(
                        JobResult(
                            original_dds=str(dds_path),
                            png="",
                            output_dir=str(target_dir),
                            width=dds_info.width,
                            height=dds_info.height,
                            original_mips=dds_info.mip_count,
                            used_mips=dds_info.mip_count,
                            texconv_format=dds_info.texconv_format,
                            status="skipped",
                            note=note,
                        )
                    )
                    emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                    emit_progress(index, total, converted, skipped, failed)
                    emit_phase_progress(index, total, f"{index} / {total} DDS files")
                    continue

                target_dir.mkdir(parents=True, exist_ok=True)
                note_parts = [
                    plan_entry.preserve_reason
                    or (
                        f"automatic policy preserved source DDS [{decision.texture_type}/{decision.semantic_subtype}]"
                        if decision.preserve_original_due_to_intermediate
                        else f"preset kept source DDS unchanged [{decision.texture_type}/{decision.semantic_subtype}]"
                    ),
                    f"planner profile={plan_entry.profile.key}",
                    f"planner path={plan_entry.path_kind}",
                    f"planner alpha_policy={plan_entry.alpha_policy}",
                    *decision.notes,
                ]
                if target_file.exists() and not normalized.overwrite_existing_dds:
                    skipped += 1
                    note = "; ".join(note_parts + ["existing DDS kept because overwrite is disabled"])
                    results.append(
                        JobResult(
                            original_dds=str(dds_path),
                            png="",
                            output_dir=str(target_dir),
                            width=dds_info.width,
                            height=dds_info.height,
                            original_mips=dds_info.mip_count,
                            used_mips=dds_info.mip_count,
                            texconv_format=dds_info.texconv_format,
                            status="skipped",
                            note=note,
                        )
                    )
                    emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                    emit_progress(index, total, converted, skipped, failed)
                    emit_phase_progress(index, total, f"{index} / {total} DDS files")
                    continue

                if normalized.dry_run:
                    converted += 1
                    status = "dry-run"
                    note = "; ".join(note_parts + ["planned DDS passthrough"])
                    emit_log(f"[{index}/{total}] DRYRUN COPY {rel_display} [{decision.texture_type}] -> original DDS passthrough")
                else:
                    shutil.copy2(dds_path, target_file)
                    converted += 1
                    status = "converted"
                    note = "; ".join(note_parts)
                    emit_log(f"[{index}/{total}] COPY {rel_display} [{decision.texture_type}] -> kept original DDS")

                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png="",
                        output_dir=str(target_dir),
                        width=dds_info.width,
                        height=dds_info.height,
                        original_mips=dds_info.mip_count,
                        used_mips=dds_info.mip_count,
                        texconv_format=dds_info.texconv_format,
                        status=status,
                        note=note,
                    )
                )
                emit_progress(index, total, converted, skipped, failed)
                emit_phase_progress(index, total, f"{index} / {total} DDS files")
                continue

            png_path, match_note = resolve_png(
                rel_path,
                relative_png_index,
                basename_png_index,
                normalized.allow_unique_basename_fallback,
            )

            if png_path is None:
                if plan_entry.path_kind == "technical_high_precision_path":
                    target_dir.mkdir(parents=True, exist_ok=True)
                    note_parts = [
                        "technical high-precision path fallback preserved the original DDS",
                        match_note,
                        f"planner profile={plan_entry.profile.key}",
                        f"planner path={plan_entry.path_kind}",
                        f"planner alpha_policy={plan_entry.alpha_policy}",
                    ]
                    if target_file.exists() and not normalized.overwrite_existing_dds:
                        skipped += 1
                        note = "; ".join(note_parts + ["existing DDS kept because overwrite is disabled"])
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png="",
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="skipped",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                    elif normalized.dry_run:
                        converted += 1
                        note = "; ".join(note_parts + ["planned DDS passthrough"])
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png="",
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="dry-run",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] DRYRUN COPY {rel_display} [{decision.texture_type}] -> high-precision PNG missing fallback")
                    else:
                        shutil.copy2(dds_path, target_file)
                        converted += 1
                        note = "; ".join(note_parts)
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png="",
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="converted",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] COPY {rel_display} [{decision.texture_type}] -> high-precision PNG missing fallback kept original DDS")
                else:
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
                high_precision_validation_message = _validate_high_precision_staged_png(png_path, plan_entry)
                if high_precision_validation_message is not None:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    note_parts = [
                        "technical high-precision path fallback preserved the original DDS",
                        high_precision_validation_message,
                        f"planner profile={plan_entry.profile.key}",
                        f"planner path={plan_entry.path_kind}",
                        f"planner alpha_policy={plan_entry.alpha_policy}",
                    ]
                    if target_file.exists() and not normalized.overwrite_existing_dds:
                        skipped += 1
                        note = "; ".join(note_parts + ["existing DDS kept because overwrite is disabled"])
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png=str(png_path),
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="skipped",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                    elif normalized.dry_run:
                        converted += 1
                        note = "; ".join(note_parts + ["planned DDS passthrough"])
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png=str(png_path),
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="dry-run",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] DRYRUN COPY {rel_display} [{decision.texture_type}] -> high-precision stage validation fallback")
                    else:
                        shutil.copy2(dds_path, target_file)
                        converted += 1
                        note = "; ".join(note_parts)
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png=str(png_path),
                                output_dir=str(target_dir),
                                width=dds_info.width,
                                height=dds_info.height,
                                original_mips=dds_info.mip_count,
                                used_mips=dds_info.mip_count,
                                texconv_format=dds_info.texconv_format,
                                status="converted",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] COPY {rel_display} [{decision.texture_type}] -> high-precision stage validation fallback kept original DDS")
                    emit_progress(index, total, converted, skipped, failed)
                    emit_phase_progress(index, total, f"{index} / {total} DDS files")
                    continue

                png_width, png_height = read_png_dimensions(png_path)
                png_has_alpha = png_has_alpha_channel(png_path)
                notes = [match_note]
                output_settings = _resolve_plan_output_settings(
                    normalized,
                    plan_entry,
                    png_width,
                    png_height,
                    has_alpha=png_has_alpha,
                )
                notes.extend(
                    [
                        f"planner profile={plan_entry.profile.key}",
                        f"planner path={plan_entry.path_kind}",
                        f"planner alpha_policy={plan_entry.alpha_policy}",
                    ]
                )
                if plan_entry.backend_capability.reason:
                    notes.append(f"planner backend={plan_entry.backend_capability.reason}")
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
                    color_args=output_settings.texconv_color_args,
                    extra_args=output_settings.texconv_extra_args,
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

    if (
        normalized.enable_mod_ready_loose_export
        and normalized.mod_ready_export_root is not None
        and not cancelled
        and failed == 0
    ):
        final_package_root = resolve_mod_package_root(normalized.mod_ready_export_root, normalized.mod_ready_package_info)
        emit_phase("Mod Package", "Writing ready mod package from final DDS output...", False)
        emit_log(f"Creating ready mod package under: {final_package_root}")
        export_result = copy_mod_ready_loose_tree(
            normalized.output_root,
            final_package_root,
            overwrite=True,
            dry_run=normalized.dry_run,
            on_log=None,
        )
        if not normalized.dry_run:
            write_mod_package_manifest(
                final_package_root,
                normalized.mod_ready_package_info,
                kind="dds_loose_mod",
                extra_fields={"file_count": export_result.copied_files},
                create_no_encrypt_file=normalized.mod_ready_create_no_encrypt_file,
                export_options=normalized.mod_ready_export_options,
            )
        emit_log(
            "Ready mod package export complete: "
            f"copied={export_result.copied_files}, skipped={export_result.skipped_files}, failed={export_result.failed_files}"
        )

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

