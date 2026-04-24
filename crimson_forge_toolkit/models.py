from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Tuple

if TYPE_CHECKING:
    from crimson_forge_toolkit.core.upscale_profiles import TextureUpscaleDecision

from crimson_forge_toolkit.constants import (
    ALLOW_UNIQUE_BASENAME_FALLBACK,
    ARCHIVE_EXTRACT_ROOT,
    ARCHIVE_EXTENSION_FILTER,
    ARCHIVE_EXCLUDE_COMMON_TECHNICAL_SUFFIXES,
    ARCHIVE_EXCLUDE_FILTER_TEXT,
    ARCHIVE_FILTER_TEXT,
    ARCHIVE_MIN_SIZE_KB,
    ARCHIVE_PACKAGE_FILTER_TEXT,
    ARCHIVE_PACKAGE_ROOT,
    ARCHIVE_PREVIEWABLE_ONLY,
    ARCHIVE_ROLE_FILTER,
    ARCHIVE_STRUCTURE_FILTER,
    CHAINNER_CHAIN_PATH,
    CHAINNER_EXE_PATH,
    CHAINNER_OVERRIDE_JSON,
    DEFAULT_UPSCALE_BACKEND,
    DEFAULT_UPSCALE_TEXTURE_PRESET,
    DDS_STAGING_ROOT,
    DEFAULT_DDS_CUSTOM_FORMAT,
    DEFAULT_DDS_CUSTOM_HEIGHT,
    DEFAULT_DDS_CUSTOM_MIP_COUNT,
    DEFAULT_DDS_CUSTOM_WIDTH,
    DEFAULT_DDS_FORMAT_MODE,
    DEFAULT_DDS_MIP_MODE,
    DEFAULT_DDS_SIZE_MODE,
    DRY_RUN,
    ENABLE_CHAINNER,
    ENABLE_AUTOMATIC_TEXTURE_RULES,
    ENABLE_UNSAFE_TECHNICAL_OVERRIDE,
    ENABLE_DDS_STAGING,
    ENABLE_INCREMENTAL_RESUME,
    ENABLE_MOD_READY_LOOSE_EXPORT,
    INCLUDE_FILTERS,
    LOG_CSV,
    MOD_READY_CREATE_NO_ENCRYPT,
    MOD_READY_EXPORT_ROOT,
    MOD_READY_PACKAGE_AUTHOR,
    MOD_READY_PACKAGE_DESCRIPTION,
    MOD_READY_PACKAGE_NEXUS_URL,
    MOD_READY_PACKAGE_TITLE,
    MOD_READY_PACKAGE_VERSION,
    ORIGINAL_DDS_ROOT,
    OUTPUT_ROOT,
    OVERWRITE_EXISTING_DDS,
    PNG_ROOT,
    TEXTURE_EDITOR_PNG_ROOT,
    REALESRGAN_NCNN_EXE_PATH,
    REALESRGAN_NCNN_MODEL_DIR,
    REALESRGAN_NCNN_MODEL_NAME,
    REALESRGAN_NCNN_SCALE,
    REALESRGAN_NCNN_TILE_SIZE,
    REALESRGAN_NCNN_EXTRA_ARGS,
    DEFAULT_UPSCALE_POST_CORRECTION,
    RETRY_SMALLER_TILE_ON_FAILURE,
    TEXCONV_PATH,
    TEXTURE_RULES_TEXT,
)


class RunCancelled(Exception):
    pass


IntermediateKind = Literal[
    "visible_color_png_path",
    "technical_preserve_path",
    "technical_high_precision_path",
]


AlphaPolicy = Literal[
    "none",
    "straight",
    "cutout_coverage",
    "channel_data",
    "premultiplied",
]


@dataclass(slots=True)
class TextureSemanticEvidence:
    items: Tuple[str, ...] = ()


@dataclass(slots=True)
class ChainnerChainAnalysis:
    node_count: int = 0
    schema_ids: List[str] = field(default_factory=list)
    load_image_dirs: List[Path] = field(default_factory=list)
    load_image_globs: List[str] = field(default_factory=list)
    load_image_recursive: List[bool] = field(default_factory=list)
    save_image_dirs: List[Path] = field(default_factory=list)
    save_image_formats: List[str] = field(default_factory=list)
    model_files: List[Path] = field(default_factory=list)
    upscaler_nodes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocking_warnings: List[str] = field(default_factory=list)
    planner_compatible: bool = True


@dataclass(slots=True)
class TextureRule:
    pattern: str
    action: str = "process"
    format_value: Optional[str] = None
    size_value: Optional[str] = None
    mip_value: Optional[str] = None
    semantic_value: Optional[str] = None
    profile_value: Optional[str] = None
    colorspace_value: Optional[str] = None
    alpha_policy_value: Optional[str] = None
    intermediate_value: Optional[str] = None
    enabled: bool = True
    match_mode: str = "glob"
    workflow_profile_id: str = ""
    source_line: str = ""


@dataclass(slots=True)
class TextureWorkflowProfile:
    profile_id: str
    label: str
    action_mode: str = ""
    format_value: Optional[str] = None
    size_value: Optional[str] = None
    mip_value: Optional[str] = None
    ncnn_model_name: str = ""
    ncnn_scale: Optional[int] = None
    ncnn_tile_size: Optional[int] = None
    ncnn_extra_args: str = ""
    post_correction_mode: str = ""


@dataclass(slots=True)
class AppConfig:
    original_dds_root: str = ORIGINAL_DDS_ROOT
    png_root: str = PNG_ROOT
    texture_editor_png_root: str = TEXTURE_EDITOR_PNG_ROOT
    output_root: str = OUTPUT_ROOT
    dds_staging_root: str = DDS_STAGING_ROOT
    texconv_path: str = TEXCONV_PATH
    dds_format_mode: str = DEFAULT_DDS_FORMAT_MODE
    dds_custom_format: str = DEFAULT_DDS_CUSTOM_FORMAT
    dds_size_mode: str = DEFAULT_DDS_SIZE_MODE
    dds_custom_width: int = DEFAULT_DDS_CUSTOM_WIDTH
    dds_custom_height: int = DEFAULT_DDS_CUSTOM_HEIGHT
    dds_mip_mode: str = DEFAULT_DDS_MIP_MODE
    dds_custom_mip_count: int = DEFAULT_DDS_CUSTOM_MIP_COUNT
    enable_dds_staging: bool = ENABLE_DDS_STAGING
    enable_incremental_resume: bool = ENABLE_INCREMENTAL_RESUME
    texture_rules_text: str = TEXTURE_RULES_TEXT
    texture_rules: Tuple[TextureRule, ...] = field(default_factory=tuple)
    workflow_profiles: Tuple[TextureWorkflowProfile, ...] = field(default_factory=tuple)
    dry_run: bool = DRY_RUN
    csv_log_enabled: bool = bool(LOG_CSV.strip())
    csv_log_path: str = LOG_CSV
    allow_unique_basename_fallback: bool = ALLOW_UNIQUE_BASENAME_FALLBACK
    overwrite_existing_dds: bool = OVERWRITE_EXISTING_DDS
    include_filters: str = INCLUDE_FILTERS
    upscale_backend: str = DEFAULT_UPSCALE_BACKEND
    enable_chainner: bool = ENABLE_CHAINNER
    chainner_exe_path: str = CHAINNER_EXE_PATH
    chainner_chain_path: str = CHAINNER_CHAIN_PATH
    chainner_override_json: str = CHAINNER_OVERRIDE_JSON
    ncnn_exe_path: str = REALESRGAN_NCNN_EXE_PATH
    ncnn_model_dir: str = REALESRGAN_NCNN_MODEL_DIR
    ncnn_model_name: str = REALESRGAN_NCNN_MODEL_NAME
    ncnn_scale: int = REALESRGAN_NCNN_SCALE
    ncnn_tile_size: int = REALESRGAN_NCNN_TILE_SIZE
    ncnn_extra_args: str = REALESRGAN_NCNN_EXTRA_ARGS
    upscale_post_correction_mode: str = DEFAULT_UPSCALE_POST_CORRECTION
    upscale_texture_preset: str = DEFAULT_UPSCALE_TEXTURE_PRESET
    enable_automatic_texture_rules: bool = ENABLE_AUTOMATIC_TEXTURE_RULES
    enable_unsafe_technical_override: bool = ENABLE_UNSAFE_TECHNICAL_OVERRIDE
    retry_smaller_tile_on_failure: bool = RETRY_SMALLER_TILE_ON_FAILURE
    enable_mod_ready_loose_export: bool = ENABLE_MOD_READY_LOOSE_EXPORT
    mod_ready_export_root: str = MOD_READY_EXPORT_ROOT
    mod_ready_create_no_encrypt_file: bool = MOD_READY_CREATE_NO_ENCRYPT
    mod_ready_package_title: str = MOD_READY_PACKAGE_TITLE
    mod_ready_package_version: str = MOD_READY_PACKAGE_VERSION
    mod_ready_package_author: str = MOD_READY_PACKAGE_AUTHOR
    mod_ready_package_description: str = MOD_READY_PACKAGE_DESCRIPTION
    mod_ready_package_nexus_url: str = MOD_READY_PACKAGE_NEXUS_URL
    archive_package_root: str = ARCHIVE_PACKAGE_ROOT
    archive_extract_root: str = ARCHIVE_EXTRACT_ROOT
    archive_filter_text: str = ARCHIVE_FILTER_TEXT
    archive_exclude_filter_text: str = ARCHIVE_EXCLUDE_FILTER_TEXT
    archive_extension_filter: str = ARCHIVE_EXTENSION_FILTER
    archive_package_filter_text: str = ARCHIVE_PACKAGE_FILTER_TEXT
    archive_structure_filter: str = ARCHIVE_STRUCTURE_FILTER
    archive_role_filter: str = ARCHIVE_ROLE_FILTER
    archive_exclude_common_technical_suffixes: bool = ARCHIVE_EXCLUDE_COMMON_TECHNICAL_SUFFIXES
    archive_min_size_kb: int = ARCHIVE_MIN_SIZE_KB
    archive_previewable_only: bool = ARCHIVE_PREVIEWABLE_ONLY


@dataclass(slots=True)
class NormalizedConfig:
    original_dds_root: Path
    png_root: Path
    texture_editor_png_root: Optional[Path]
    output_root: Path
    dds_staging_root: Optional[Path]
    texconv_path: Path
    dds_format_mode: str
    dds_custom_format: str
    dds_size_mode: str
    dds_custom_width: int
    dds_custom_height: int
    dds_mip_mode: str
    dds_custom_mip_count: int
    enable_dds_staging: bool
    enable_incremental_resume: bool
    texture_rules_text: str
    texture_rules: Tuple[TextureRule, ...]
    workflow_profiles: Tuple[TextureWorkflowProfile, ...]
    dry_run: bool
    csv_log_path: Optional[Path]
    allow_unique_basename_fallback: bool
    overwrite_existing_dds: bool
    include_filter_patterns: Tuple[str, ...]
    upscale_backend: str
    enable_chainner: bool
    chainner_exe_path: Optional[Path]
    chainner_chain_path: Optional[Path]
    chainner_override_json: str
    ncnn_exe_path: Optional[Path]
    ncnn_model_dir: Optional[Path]
    ncnn_model_name: str
    ncnn_scale: int
    ncnn_tile_size: int
    ncnn_extra_args: str
    upscale_post_correction_mode: str
    upscale_texture_preset: str
    enable_automatic_texture_rules: bool
    enable_unsafe_technical_override: bool
    retry_smaller_tile_on_failure: bool
    enable_mod_ready_loose_export: bool
    mod_ready_export_root: Optional[Path]
    mod_ready_create_no_encrypt_file: bool
    mod_ready_package_info: ModPackageInfo


@dataclass(slots=True)
class DdsInfo:
    width: int
    height: int
    mip_count: int
    texconv_format: str
    source_path: Path
    has_alpha: bool = False
    colorspace_intent: str = "unknown"
    precision_sensitive: bool = False
    packed_channel_risk: bool = False
    preserve_only_source: bool = False


@dataclass(slots=True)
class DdsOutputSettings:
    texconv_format: str
    mip_count: int
    width: int
    height: int
    resize_to_dimensions: bool
    notes: List[str] = field(default_factory=list)
    texconv_color_args: List[str] = field(default_factory=list)
    texconv_extra_args: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TextureProcessingProfile:
    key: str
    label: str
    allowed_intermediate_kinds: Tuple[IntermediateKind, ...]
    preferred_texconv_format: str
    colorspace_policy: str
    alpha_policy: AlphaPolicy
    mip_policy_hint: str
    preserve_only: bool = False


@dataclass(slots=True)
class TextureWorkflowDdsOverride:
    format_value: Optional[str] = None
    size_value: Optional[str] = None
    mip_value: Optional[str] = None


@dataclass(slots=True)
class EffectiveNcnnSettings:
    model_name: str = ""
    scale: int = 0
    tile_size: int = 0
    extra_args: str = ""
    post_correction_mode: str = ""


@dataclass(slots=True)
class BackendCapabilityDecision:
    backend: str
    path_kind: IntermediateKind | str
    compatible: bool
    execution_mode: str
    reason: str


@dataclass(slots=True)
class BackendCapabilityMatrix:
    backend: str
    decisions_by_path_kind: Dict[str, BackendCapabilityDecision] = field(default_factory=dict)
    planner_notes: Tuple[str, ...] = ()

    def decision_for(self, path_kind: str) -> BackendCapabilityDecision:
        return self.decisions_by_path_kind.get(
            path_kind,
            BackendCapabilityDecision(
                backend=self.backend,
                path_kind=path_kind,
                compatible=False,
                execution_mode="preserve_original",
                reason=f"Unsupported planner path kind: {path_kind}",
            ),
        )


@dataclass(slots=True)
class TextureProcessingPlan:
    dds_path: Path
    relative_path: Path
    dds_info: DdsInfo
    decision: "TextureUpscaleDecision"
    action: str
    action_reason: str
    path_kind: IntermediateKind | str
    intermediate_kind: IntermediateKind | str
    profile: TextureProcessingProfile
    alpha_policy: AlphaPolicy | str
    backend_capability: BackendCapabilityDecision
    requires_png_processing: bool
    preserve_reason: str = ""
    lossy_intermediate_warning: str = ""
    matched_rule: Optional[TextureRule] = None
    workflow_profile: Optional[TextureWorkflowProfile] = None
    effective_output_override: TextureWorkflowDdsOverride = field(default_factory=TextureWorkflowDdsOverride)
    effective_ncnn_settings: EffectiveNcnnSettings = field(default_factory=EffectiveNcnnSettings)
    semantic_evidence: TextureSemanticEvidence = field(default_factory=TextureSemanticEvidence)


@dataclass(slots=True)
class ArchiveEntry:
    path: str
    pamt_path: Path
    paz_file: Path
    offset: int
    comp_size: int
    orig_size: int
    flags: int
    paz_index: int

    @property
    def extension(self) -> str:
        path = self.path
        slash_index = max(path.rfind("/"), path.rfind("\\"))
        dot_index = path.rfind(".")
        if dot_index <= slash_index:
            return ""
        return path[dot_index:].lower()

    @property
    def basename(self) -> str:
        path = self.path
        slash_index = max(path.rfind("/"), path.rfind("\\"))
        return path[slash_index + 1 :]

    @property
    def compressed(self) -> bool:
        return self.comp_size != self.orig_size

    @property
    def compression_type(self) -> int:
        return self.flags & 0x0F

    @property
    def compression_label(self) -> str:
        return {
            0: "None",
            1: "Partial",
            2: "LZ4",
            3: "Zlib",
            4: "QuickLZ",
        }.get(self.compression_type, str(self.compression_type))

    @property
    def encrypted(self) -> bool:
        return (self.flags >> 4) != 0

    @property
    def encryption_type(self) -> int:
        return (self.flags >> 4) & 0x0F

    @property
    def encryption_label(self) -> str:
        return {
            0: "None",
            1: "ICE",
            2: "AES",
            3: "ChaCha20",
        }.get(self.encryption_type, str(self.encryption_type))

    @property
    def package_label(self) -> str:
        return f"{self.pamt_path.parent.name}/{self.pamt_path.name}"


@dataclass(slots=True)
class JobResult:
    original_dds: str
    png: str
    output_dir: str
    width: int
    height: int
    original_mips: int
    used_mips: int
    texconv_format: str
    status: str
    note: str


@dataclass(slots=True)
class ScanResult:
    total_files: int
    files: List[Path]


@dataclass(slots=True)
class RunSummary:
    total_files: int
    converted: int
    skipped: int
    failed: int
    cancelled: bool = False
    log_csv_path: Optional[Path] = None
    results: List[JobResult] = field(default_factory=list)


@dataclass(slots=True)
class MatchedOriginalTexture:
    package_root: str
    archive_relative_path: str
    loose_relative_path: Path
    original_dds_path: Optional[Path] = None
    archive_entry: Optional["ArchiveEntry"] = None
    match_reason: str = ""


@dataclass(slots=True)
class ReplaceAssistantItem:
    source_path: Path
    source_kind: str
    detected_relative_path: str = ""
    detected_package_root: str = ""
    matched_original: Optional[MatchedOriginalTexture] = None
    warning: str = ""
    status: str = "pending"
    status_detail: str = ""


@dataclass(slots=True)
class ModPackageInfo:
    title: str = MOD_READY_PACKAGE_TITLE
    version: str = MOD_READY_PACKAGE_VERSION
    author: str = MOD_READY_PACKAGE_AUTHOR
    description: str = MOD_READY_PACKAGE_DESCRIPTION
    nexus_url: str = MOD_READY_PACKAGE_NEXUS_URL


@dataclass(slots=True)
class ReplaceAssistantBuildOptions:
    package_output_root: Path
    overwrite_existing_package_files: bool
    create_no_encrypt_file: bool
    build_mode: str
    size_mode: str
    texconv_path: Path
    ncnn_exe_path: Optional[Path]
    ncnn_model_dir: Optional[Path]
    ncnn_model_name: str
    ncnn_scale: int
    ncnn_tile_size: int
    ncnn_extra_args: str
    retry_smaller_tile_on_failure: bool
    upscale_post_correction_mode: str
    upscale_texture_preset: str
    enable_automatic_texture_rules: bool
    enable_unsafe_technical_override: bool
    package_info: ModPackageInfo


@dataclass(slots=True)
class ReplaceAssistantReviewItem:
    source_path: Path
    relative_path: Path
    output_dds_path: Path
    original_dds_path: Optional[Path] = None
    build_mode: str = ""
    size_mode: str = ""


@dataclass(slots=True)
class ReplaceAssistantBuildSummary:
    total_items: int
    built_items: int
    skipped_items: int
    unresolved_items: int
    failed_items: int
    cancelled: bool = False
    output_root: Optional[Path] = None
    review_items: Tuple[ReplaceAssistantReviewItem, ...] = ()


@dataclass(slots=True)
class TextureEditorSourceBinding:
    launch_origin: str = ""
    display_name: str = ""
    source_path: str = ""
    source_identity_path: str = ""
    relative_path: str = ""
    package_root: str = ""
    archive_relative_path: str = ""
    original_dds_path: str = ""
    original_texconv_format: str = ""
    texture_type: str = "unknown"
    semantic_subtype: str = "unknown"
    technical_warning: str = ""
    semantic_sidecar_texts: Tuple[str, ...] = ()


@dataclass(slots=True)
class TextureEditorLayer:
    layer_id: str
    name: str
    relative_png_path: str
    visible: bool = True
    opacity: int = 100
    blend_mode: str = "normal"
    offset_x: int = 0
    offset_y: int = 0
    locked: bool = False
    alpha_locked: bool = False
    mask_layer_id: str = ""
    mask_enabled: bool = True
    revision: int = 0
    thumbnail_cache_key: str = ""


@dataclass(slots=True)
class TextureEditorSelection:
    mode: str = "none"
    rect: Optional[Tuple[int, int, int, int]] = None
    polygon_points: Tuple[Tuple[float, float], ...] = ()
    mask_polygons: Tuple[Tuple[Tuple[float, float], ...], ...] = ()
    mask_png_blob: bytes = b""
    inverted: bool = False
    feather_radius: int = 0


@dataclass(slots=True)
class TextureEditorFloatingSelection:
    source_layer_id: str = ""
    label: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    offset_x: int = 0
    offset_y: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_degrees: float = 0.0
    flip_x: bool = False
    flip_y: bool = False
    paste_mode: str = "in_place"
    committed: bool = True


@dataclass(slots=True)
class TextureEditorToolSettings:
    tool: str = "paint"
    color_hex: str = "#C85A30"
    secondary_color_hex: str = "#FFFFFF"
    brush_preset: str = "custom"
    brush_tip: str = "round"
    brush_pattern: str = "solid"
    custom_brush_tip_path: str = ""
    symmetry_mode: str = "off"
    size_step_mode: str = "normal"
    paint_blend_mode: str = "normal"
    size: float = 32.0
    hardness: int = 80
    opacity: int = 100
    flow: int = 100
    spacing: int = 20
    roundness: int = 100
    angle_degrees: int = 0
    smoothing: int = 0
    strength: int = 50
    sharpen_mode: str = "unsharp_mask"
    soften_mode: str = "gaussian"
    smudge_strength: int = 45
    dodge_burn_mode: str = "dodge_midtones"
    dodge_burn_exposure: int = 20
    patch_blend: int = 70
    gradient_type: str = "linear"
    sample_visible_layers: bool = True
    clone_aligned: bool = True
    fill_tolerance: int = 24
    fill_contiguous: bool = True
    clone_source_point: Optional[Tuple[int, int]] = None
    selection_combine_mode: str = "replace"
    lasso_snap_to_edges: bool = False
    lasso_snap_radius: int = 10
    lasso_edge_sensitivity: int = 55
    recolor_mode: str = "tint"
    recolor_source_hex: str = "#808080"
    recolor_target_hex: str = "#C85A30"
    recolor_tolerance: int = 48
    recolor_strength: int = 100
    recolor_preserve_luminance: bool = True


@dataclass(slots=True)
class TextureEditorHistoryEntry:
    label: str
    timestamp: float = 0.0


@dataclass(slots=True)
class TextureEditorCommand:
    kind: str
    label: str
    timestamp: float = 0.0
    dirty_bounds: Optional[Tuple[int, int, int, int]] = None
    checkpoint: bool = False


@dataclass(slots=True)
class TextureEditorAdjustmentLayer:
    layer_id: str
    name: str
    adjustment_type: str
    enabled: bool = True
    opacity: int = 100
    parameters: Dict[str, float] = field(default_factory=dict)
    mask_layer_id: str = ""
    revision: int = 0


@dataclass(slots=True)
class TextureEditorDocument:
    title: str
    width: int
    height: int
    project_path: Optional[Path] = None
    workspace_root: Optional[Path] = None
    active_layer_id: str = ""
    layers: Tuple[TextureEditorLayer, ...] = ()
    source_binding: TextureEditorSourceBinding = field(default_factory=TextureEditorSourceBinding)
    selection: TextureEditorSelection = field(default_factory=TextureEditorSelection)
    floating_selection: Optional[TextureEditorFloatingSelection] = None
    adjustment_layers: Tuple[TextureEditorAdjustmentLayer, ...] = ()
    technical_warning: str = ""
    last_flattened_png_path: str = ""
    composite_revision: int = 0
    quick_mask_enabled: bool = False
    edit_red_channel: bool = True
    edit_green_channel: bool = True
    edit_blue_channel: bool = True
    edit_alpha_channel: bool = True


@dataclass(slots=True)
class TextureEditorWorkspace:
    open_document_ids: Tuple[str, ...] = ()
    active_document_id: str = ""
    clipboard_kind: str = ""
    document_view_state: Dict[str, Dict[str, object]] = field(default_factory=dict)


@dataclass(slots=True)
class ComparePreviewPaneResult:
    status: str
    title: str = ""
    message: str = ""
    preview_png_path: str = ""
    preview_image: object = None
    metadata_summary: str = ""


class RelationKind(str, Enum):
    MESH = "mesh"
    LOD = "lod"
    MATERIAL_SIDECAR = "material_sidecar"
    TEXTURE = "texture"
    SKELETON = "skeleton"
    ANIMATION = "animation"
    METADATA = "metadata"


class RelationConfidence(str, Enum):
    AUTHORITATIVE = "authoritative"
    DERIVED_SAME_STEM = "derived_same_stem"
    DERIVED_FAMILY_HEURISTIC = "derived_family_heuristic"


class ImportIssueStatus(str, Enum):
    AUTO_FIXED = "auto-fixed"
    WARNING = "warning"
    REQUIRES_MANUAL_REVIEW = "requires-manual-review"


@dataclass(slots=True)
class AssetRelation:
    source_path: str = ""
    target_path: str = ""
    relation_kind: str = RelationKind.METADATA.value
    confidence: str = RelationConfidence.DERIVED_SAME_STEM.value
    role_label: str = ""
    reason: str = ""
    source_entry: Optional["ArchiveEntry"] = None
    target_entry: Optional["ArchiveEntry"] = None
    semantic_label: str = ""
    semantic_hint: str = ""
    sidecar_parameter_name: str = ""
    material_name: str = ""
    package_label: str = ""


@dataclass(slots=True)
class AssetFamilyGraph:
    root_path: str = ""
    family_key: str = ""
    members: Tuple[str, ...] = ()
    relations: Tuple[AssetRelation, ...] = ()
    grouped_paths: Dict[str, Tuple[str, ...]] = field(default_factory=dict)


@dataclass(slots=True)
class ArchivePreviewResult:
    status: str
    title: str = ""
    metadata_summary: str = ""
    detail_text: str = ""
    timings: Dict[str, float] = field(default_factory=dict)
    timing_summary: str = ""
    preview_image_path: str = ""
    preview_image: object = None
    preview_media_path: str = ""
    preview_media_kind: str = ""
    preview_text: str = ""
    preview_model: object = None
    prepared_preview_model: Optional["PreparedModelPreviewData"] = None
    model_texture_references: Tuple["ArchiveModelTextureReference", ...] = ()
    asset_family_graph: Optional[AssetFamilyGraph] = None
    preferred_view: str = "info"
    warning_badge: str = ""
    warning_text: str = ""
    loose_file_path: str = ""
    loose_preview_image_path: str = ""
    loose_preview_image: object = None
    loose_preview_media_path: str = ""
    loose_preview_media_kind: str = ""
    loose_preview_title: str = ""
    loose_preview_metadata_summary: str = ""
    loose_preview_detail_text: str = ""


@dataclass(slots=True)
class ModelPreviewMesh:
    material_name: str = ""
    texture_name: str = ""
    preview_color: Tuple[float, float, float] = ()
    positions: List[Tuple[float, float, float]] = field(default_factory=list)
    texture_coordinates: List[Tuple[float, float]] = field(default_factory=list)
    normals: List[Tuple[float, float, float]] = field(default_factory=list)
    indices: List[int] = field(default_factory=list)
    preview_texture_path: str = ""
    preview_texture_image: object = None
    preview_normal_texture_path: str = ""
    preview_normal_texture_image: object = None
    preview_normal_texture_name: str = ""
    preview_normal_texture_strength: float = 0.0
    preview_material_texture_path: str = ""
    preview_material_texture_image: object = None
    preview_material_texture_name: str = ""
    preview_material_texture_type: str = ""
    preview_material_texture_subtype: str = ""
    preview_material_texture_packed_channels: Tuple[str, ...] = ()
    preview_height_texture_path: str = ""
    preview_height_texture_image: object = None
    preview_height_texture_name: str = ""
    preview_base_texture_default_path: str = ""
    preview_base_texture_default_name: str = ""
    preview_normal_texture_default_path: str = ""
    preview_normal_texture_default_name: str = ""
    preview_normal_texture_default_strength: float = 0.0
    preview_material_texture_default_path: str = ""
    preview_material_texture_default_name: str = ""
    preview_material_texture_default_type: str = ""
    preview_material_texture_default_subtype: str = ""
    preview_material_texture_default_packed_channels: Tuple[str, ...] = ()
    preview_height_texture_default_path: str = ""
    preview_height_texture_default_name: str = ""
    preview_texture_flip_vertical: Optional[bool] = None
    preview_debug_flip_base_v: bool = False
    preview_debug_disable_support_maps: bool = False


@dataclass(slots=True)
class ArchiveModelTextureReference:
    reference_name: str = ""
    material_name: str = ""
    semantic_label: str = ""
    semantic_hint: str = ""
    sidecar_parameter_name: str = ""
    sidecar_texts: Tuple[str, ...] = ()
    resolution_status: str = "missing"
    resolved_archive_path: str = ""
    resolved_package_label: str = ""
    resolved_entry: Optional["ArchiveEntry"] = None
    preview_texture_path: str = ""
    usage_count: int = 0
    reference_kind: str = "texture"
    relation_group: str = "Textures"
    relation_reason: str = ""
    relation_confidence: str = RelationConfidence.DERIVED_SAME_STEM.value


@dataclass(slots=True)
class ModelPreviewData:
    path: str = ""
    format: str = ""
    summary: str = ""
    mesh_count: int = 0
    vertex_count: int = 0
    face_count: int = 0
    lod_index: int = -1
    lod_count: int = 0
    normalization_center: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    normalization_scale: float = 1.0
    meshes: List[ModelPreviewMesh] = field(default_factory=list)


@dataclass(slots=True)
class PreparedModelPreviewBatch:
    material_name: str = ""
    texture_name: str = ""
    vertex_blob: bytes = b""
    index_count: int = 0
    preview_texture_path: str = ""
    preview_normal_texture_path: str = ""
    preview_material_texture_path: str = ""
    preview_height_texture_path: str = ""
    preview_texture_flip_vertical: Optional[bool] = None
    preview_normal_texture_strength: float = 0.0
    preview_material_texture_type: str = ""
    preview_material_texture_subtype: str = ""
    preview_material_texture_packed_channels: Tuple[str, ...] = ()
    has_texture_coordinates: bool = False
    texture_wrap_repeat: bool = False
    preview_debug_flip_base_v: bool = False
    preview_debug_disable_support_maps: bool = False


@dataclass(slots=True)
class PreparedModelPreviewData:
    source_path: str = ""
    format: str = ""
    summary: str = ""
    mesh_count: int = 0
    vertex_count: int = 0
    face_count: int = 0
    lod_index: int = -1
    lod_count: int = 0
    normalization_center: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    normalization_scale: float = 1.0
    batches: Tuple[PreparedModelPreviewBatch, ...] = ()


@dataclass(slots=True)
class MeshImportDiff:
    field_name: str = ""
    original_value: str = ""
    imported_value: str = ""
    severity: str = ""
    safe_to_auto_fix: bool = False
    detail: str = ""


@dataclass(slots=True)
class ImportIssue:
    code: str = ""
    title: str = ""
    status: str = ImportIssueStatus.WARNING.value
    detail: str = ""
    diffs: Tuple[MeshImportDiff, ...] = ()


@dataclass(slots=True)
class ImportAutoFixResult:
    applied_fields: Tuple[str, ...] = ()
    warning_fields: Tuple[str, ...] = ()
    manual_review_fields: Tuple[str, ...] = ()
    issues: Tuple[ImportIssue, ...] = ()


MODEL_PREVIEW_RENDER_LIMITS: Dict[str, Tuple[float, float]] = {
    "preview_texture_max_dimension": (1024.0, 16384.0),
    "low_quality_texture_max_dimension": (128.0, 4096.0),
    "max_anisotropy": (1.0, 16.0),
    "ambient_strength": (0.35, 1.0),
    "diffuse_wrap_bias": (0.20, 1.0),
    "diffuse_light_scale": (0.20, 1.0),
    "orbit_sensitivity": (0.05, 1.0),
    "pan_sensitivity": (0.05, 3.0),
    "normal_strength_cap": (0.0, 1.0),
    "normal_strength_floor": (0.0, 1.0),
    "height_effect_max": (0.0, 1.0),
    "cavity_clamp_min": (0.70, 1.0),
    "cavity_clamp_max": (1.0, 2.0),
    "specular_base": (0.0, 0.5),
    "specular_min": (0.0, 0.5),
    "specular_max": (0.0, 1.0),
    "shininess_base": (1.0, 128.0),
    "shininess_min": (1.0, 128.0),
    "shininess_max": (1.0, 256.0),
    "height_shininess_boost": (0.0, 64.0),
}

MODEL_PREVIEW_VISIBLE_TEXTURE_MODES: Tuple[str, ...] = (
    "mesh_base_first",
    "layer_aware_visible",
    "sidecar_visible_first",
)

MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS: Dict[str, str] = {
    "mesh_base_first": "Mesh Base First",
    "layer_aware_visible": "Layer-Aware Visible",
    "sidecar_visible_first": "Sidecar Visible First",
}


@dataclass(slots=True)
class ModelPreviewRenderSettings:
    use_textures_by_default: bool = True
    high_quality_by_default: bool = True
    visible_texture_mode: str = "mesh_base_first"
    preview_texture_max_dimension: int = 16384
    low_quality_texture_max_dimension: int = 2048
    max_anisotropy: int = 16
    ambient_strength: float = 0.55
    diffuse_wrap_bias: float = 0.60
    diffuse_light_scale: float = 0.65
    orbit_sensitivity: float = 0.22
    pan_sensitivity: float = 0.60
    invert_orbit_x: bool = False
    invert_orbit_y: bool = False
    invert_pan_x: bool = False
    invert_pan_y: bool = False
    normal_strength_cap: float = 1.00
    normal_strength_floor: float = 0.50
    height_effect_max: float = 0.35
    cavity_clamp_min: float = 0.75
    cavity_clamp_max: float = 1.25
    specular_base: float = 0.050
    specular_min: float = 0.050
    specular_max: float = 0.18
    shininess_base: float = 36.0
    shininess_min: float = 28.0
    shininess_max: float = 72.0
    height_shininess_boost: float = 16.0

def clamp_model_preview_render_settings(
    settings: Optional[ModelPreviewRenderSettings] = None,
) -> ModelPreviewRenderSettings:
    if settings is None:
        value = ModelPreviewRenderSettings()
    else:
        value = ModelPreviewRenderSettings(
            **{field_info.name: getattr(settings, field_info.name) for field_info in fields(ModelPreviewRenderSettings)}
        )

    int_fields = {
        "preview_texture_max_dimension",
        "low_quality_texture_max_dimension",
        "max_anisotropy",
    }
    for field_name, (minimum, maximum) in MODEL_PREVIEW_RENDER_LIMITS.items():
        raw_value = getattr(value, field_name)
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            numeric_value = float(getattr(ModelPreviewRenderSettings(), field_name))
        clamped_value = max(minimum, min(maximum, numeric_value))
        if field_name in int_fields:
            setattr(value, field_name, int(round(clamped_value)))
        else:
            setattr(value, field_name, float(clamped_value))

    value.normal_strength_floor = min(value.normal_strength_floor, value.normal_strength_cap)
    value.cavity_clamp_min = min(value.cavity_clamp_min, value.cavity_clamp_max)
    value.specular_min = min(value.specular_min, value.specular_max)
    value.shininess_min = min(value.shininess_min, value.shininess_max)
    normalized_visible_texture_mode = str(getattr(value, "visible_texture_mode", "") or "").strip().lower()
    if normalized_visible_texture_mode not in MODEL_PREVIEW_VISIBLE_TEXTURE_MODES:
        normalized_visible_texture_mode = ModelPreviewRenderSettings().visible_texture_mode
    value.visible_texture_mode = normalized_visible_texture_mode
    return value


@dataclass
class PathcEntry:
    texture_header_index: int
    collision_start_index: int
    collision_end_index: int
    compressed_block_infos: bytes
    checksum: int = 0


@dataclass
class PathcCollisionEntry:
    filename_offset: int
    texture_header_index: int
    unknown0: int
    compressed_block_infos: bytes
    path: str = ""


@dataclass
class PathcLookupResult:
    normalized_path: str
    checksum: int
    mapping_mode: str
    texture_header_index: int = -1
    header_size: int = 0
    compressed_block_infos: bytes = b""
    collision_path: str = ""
    message: str = ""


def default_config() -> AppConfig:
    return AppConfig()
