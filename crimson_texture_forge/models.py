from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from crimson_texture_forge.constants import (
    ALLOW_UNIQUE_BASENAME_FALLBACK,
    ARCHIVE_EXTRACT_ROOT,
    ARCHIVE_EXTENSION_FILTER,
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
    ENABLE_DDS_STAGING,
    ENABLE_INCREMENTAL_RESUME,
    INCLUDE_FILTERS,
    LOG_CSV,
    ORIGINAL_DDS_ROOT,
    OUTPUT_ROOT,
    OVERWRITE_EXISTING_DDS,
    PNG_ROOT,
    TEXCONV_PATH,
    TEXTURE_RULES_TEXT,
)


class RunCancelled(Exception):
    pass


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


@dataclass(slots=True)
class TextureRule:
    pattern: str
    action: str = "process"
    format_value: Optional[str] = None
    size_value: Optional[str] = None
    mip_value: Optional[str] = None
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
    enable_chainner: bool = ENABLE_CHAINNER
    chainner_exe_path: str = CHAINNER_EXE_PATH
    chainner_chain_path: str = CHAINNER_CHAIN_PATH
    chainner_override_json: str = CHAINNER_OVERRIDE_JSON
    archive_package_root: str = ARCHIVE_PACKAGE_ROOT
    archive_extract_root: str = ARCHIVE_EXTRACT_ROOT
    archive_filter_text: str = ARCHIVE_FILTER_TEXT
    archive_extension_filter: str = ARCHIVE_EXTENSION_FILTER
    archive_package_filter_text: str = ARCHIVE_PACKAGE_FILTER_TEXT
    archive_structure_filter: str = ARCHIVE_STRUCTURE_FILTER
    archive_role_filter: str = ARCHIVE_ROLE_FILTER
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
    enable_chainner: bool
    chainner_exe_path: Optional[Path]
    chainner_chain_path: Optional[Path]
    chainner_override_json: str


@dataclass(slots=True)
class DdsInfo:
    width: int
    height: int
    mip_count: int
    texconv_format: str
    source_path: Path


@dataclass(slots=True)
class DdsOutputSettings:
    texconv_format: str
    mip_count: int
    width: int
    height: int
    resize_to_dimensions: bool
    notes: List[str] = field(default_factory=list)


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
