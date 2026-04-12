from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from crimson_texture_forge.constants import (
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
    MOD_READY_EXPORT_ROOT,
    ONNX_MODEL_DIR,
    ONNX_MODEL_NAME,
    ORIGINAL_DDS_ROOT,
    OUTPUT_ROOT,
    OVERWRITE_EXISTING_DDS,
    PNG_ROOT,
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
    source_line: str = ""


@dataclass(slots=True)
class AppConfig:
    original_dds_root: str = ORIGINAL_DDS_ROOT
    png_root: str = PNG_ROOT
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
    onnx_model_dir: str = ONNX_MODEL_DIR
    onnx_model_name: str = ONNX_MODEL_NAME
    upscale_texture_preset: str = DEFAULT_UPSCALE_TEXTURE_PRESET
    enable_automatic_texture_rules: bool = ENABLE_AUTOMATIC_TEXTURE_RULES
    enable_unsafe_technical_override: bool = ENABLE_UNSAFE_TECHNICAL_OVERRIDE
    retry_smaller_tile_on_failure: bool = RETRY_SMALLER_TILE_ON_FAILURE
    enable_mod_ready_loose_export: bool = ENABLE_MOD_READY_LOOSE_EXPORT
    mod_ready_export_root: str = MOD_READY_EXPORT_ROOT
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
    onnx_model_dir: Optional[Path]
    onnx_model_name: str
    upscale_texture_preset: str
    enable_automatic_texture_rules: bool
    enable_unsafe_technical_override: bool
    retry_smaller_tile_on_failure: bool
    enable_mod_ready_loose_export: bool
    mod_ready_export_root: Optional[Path]


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
class ComparePreviewPaneResult:
    status: str
    title: str = ""
    message: str = ""
    preview_png_path: str = ""
    preview_image: object = None
    metadata_summary: str = ""


@dataclass(slots=True)
class ArchivePreviewResult:
    status: str
    title: str = ""
    metadata_summary: str = ""
    detail_text: str = ""
    preview_image_path: str = ""
    preview_text: str = ""
    preferred_view: str = "info"
    warning_badge: str = ""
    warning_text: str = ""
    loose_file_path: str = ""
    loose_preview_image_path: str = ""
    loose_preview_title: str = ""
    loose_preview_metadata_summary: str = ""
    loose_preview_detail_text: str = ""


@dataclass
class PathcEntry:
    texture_header_index: int
    collision_start_index: int
    collision_end_index: int
    compressed_block_infos: bytes


@dataclass
class PathcCollisionEntry:
    filename_offset: int
    texture_header_index: int
    unknown0: int
    compressed_block_infos: bytes


def default_config() -> AppConfig:
    return AppConfig()
