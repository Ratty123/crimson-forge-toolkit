from __future__ import annotations

import re

ORIGINAL_DDS_ROOT = ""
PNG_ROOT = ""
OUTPUT_ROOT = ""
TEXCONV_PATH = ""

DRY_RUN = False
LOG_CSV = ""
ALLOW_UNIQUE_BASENAME_FALLBACK = True

OVERWRITE_EXISTING_DDS = True
INCLUDE_FILTERS = ""
ENABLE_CHAINNER = False
CHAINNER_EXE_PATH = ""
CHAINNER_CHAIN_PATH = ""
CHAINNER_OVERRIDE_JSON = ""

DDS_FORMAT_MODE_MATCH_ORIGINAL = "match_original"
DDS_FORMAT_MODE_CUSTOM = "custom"
DDS_SIZE_MODE_PNG = "png"
DDS_SIZE_MODE_ORIGINAL = "original"
DDS_SIZE_MODE_CUSTOM = "custom"
DDS_MIP_MODE_MATCH_ORIGINAL = "match_original"
DDS_MIP_MODE_FULL_CHAIN = "full_chain"
DDS_MIP_MODE_SINGLE = "single"
DDS_MIP_MODE_CUSTOM = "custom"

DEFAULT_DDS_FORMAT_MODE = DDS_FORMAT_MODE_MATCH_ORIGINAL
DEFAULT_DDS_CUSTOM_FORMAT = "BC7_UNORM"
DEFAULT_DDS_SIZE_MODE = DDS_SIZE_MODE_PNG
DEFAULT_DDS_CUSTOM_WIDTH = 2048
DEFAULT_DDS_CUSTOM_HEIGHT = 2048
DEFAULT_DDS_MIP_MODE = DDS_MIP_MODE_MATCH_ORIGINAL
DEFAULT_DDS_CUSTOM_MIP_COUNT = 1
ENABLE_DDS_STAGING = False
DDS_STAGING_ROOT = ""
ENABLE_INCREMENTAL_RESUME = False
TEXTURE_RULES_TEXT = ""
ARCHIVE_PACKAGE_ROOT = ""
ARCHIVE_EXTRACT_ROOT = ""
ARCHIVE_FILTER_TEXT = ""
ARCHIVE_EXTENSION_FILTER = ".dds"
ARCHIVE_PACKAGE_FILTER_TEXT = ""
ARCHIVE_STRUCTURE_FILTER = ""
ARCHIVE_ROLE_FILTER = "all"
ARCHIVE_MIN_SIZE_KB = 0
ARCHIVE_PREVIEWABLE_ONLY = False
ARCHIVE_SCAN_CACHE_DIRNAME = "archive_cache"
ARCHIVE_IMAGE_EXTENSIONS = {
    ".bmp",
    ".dds",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".png",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
}
ARCHIVE_TEXT_EXTENSIONS = {
    ".cfg",
    ".csv",
    ".dae",
    ".gltf",
    ".h",
    ".hpp",
    ".ini",
    ".json",
    ".log",
    ".lua",
    ".material",
    ".mtl",
    ".obj",
    ".shader",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
ARCHIVE_MODEL_EXTENSIONS = {
    ".3ds",
    ".dae",
    ".fbx",
    ".glb",
    ".gltf",
    ".mesh",
    ".mdl",
    ".model",
    ".obj",
    ".pac",
    ".pam",
    ".pat",
    ".patx",
}
ARCHIVE_TEXT_PREVIEW_LIMIT = 240_000
ARCHIVE_BINARY_HEX_PREVIEW_LIMIT = 256

APP_ORGANIZATION = "Ratrider"
APP_NAME = "CrimsonTextureForge"
APP_TITLE = "Crimson Texture Forge"
APP_VERSION = "0.2.0"
CRIMSON_DESERT_STEAM_APP_ID = "3321460"
DEFAULT_UI_THEME = "graphite"
CHAINNER_SETTLE_SECONDS = 2.0
CHAINNER_ENV_VARS_TO_REMOVE = ("ELECTRON_RUN_AS_NODE",)
CHAINNER_PROGRESS_RE = re.compile(r"Executed\s+(\d+)\s*/\s*(\d+)\s+nodes", re.IGNORECASE)
CHAINNER_NO_VALID_IMAGES_RE = re.compile(
    r"(?P<directory>[A-Za-z]:[\\/][^\r\n]+?)\s+has\s+no\s+valid\s+images",
    re.IGNORECASE,
)
CHAINNER_WINDOWS_PORTABLE_RE = re.compile(
    r"https://github\.com/chaiNNer-org/chaiNNer/releases/download/[^\"'\s>]+/chaiNNer-windows-x64-[^\"'\s>]+-portable\.zip",
    re.IGNORECASE,
)
SUPPORTED_CHAINNER_LOAD_IMAGE_SUFFIXES = {
    ".bmp",
    ".dds",
    ".exr",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".jp2",
    ".j2k",
    ".png",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
}
DIRECTXTEX_RELEASES_API_URL = "https://api.github.com/repos/microsoft/DirectXTex/releases"

DDS_MAGIC = b"DDS "
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

DDPF_ALPHAPIXELS = 0x1
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000

DXGI_TO_TEXCONV = {
    28: "R8G8B8A8_UNORM",
    29: "R8G8B8A8_UNORM_SRGB",
    71: "BC1_UNORM",
    72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM",
    75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM",
    78: "BC3_UNORM_SRGB",
    80: "BC4_UNORM",
    81: "BC4_SNORM",
    83: "BC5_UNORM",
    84: "BC5_SNORM",
    87: "B8G8R8A8_UNORM",
    91: "B8G8R8A8_UNORM_SRGB",
    95: "BC6H_UF16",
    96: "BC6H_SF16",
    98: "BC7_UNORM",
    99: "BC7_UNORM_SRGB",
}

LEGACY_FOURCC_TO_TEXCONV = {
    b"DXT1": "BC1_UNORM",
    b"DXT3": "BC2_UNORM",
    b"DXT5": "BC3_UNORM",
    b"ATI1": "BC4_UNORM",
    b"BC4U": "BC4_UNORM",
    b"BC4S": "BC4_SNORM",
    b"ATI2": "BC5_UNORM",
    b"BC5U": "BC5_UNORM",
    b"BC5S": "BC5_SNORM",
}

SUPPORTED_TEXCONV_FORMAT_CHOICES = (
    "R8G8B8A8_UNORM",
    "R8G8B8A8_UNORM_SRGB",
    "B8G8R8A8_UNORM",
    "B8G8R8A8_UNORM_SRGB",
    "B8G8R8X8_UNORM",
    "BC1_UNORM",
    "BC1_UNORM_SRGB",
    "BC2_UNORM",
    "BC2_UNORM_SRGB",
    "BC3_UNORM",
    "BC3_UNORM_SRGB",
    "BC4_UNORM",
    "BC4_SNORM",
    "BC5_UNORM",
    "BC5_SNORM",
    "BC6H_UF16",
    "BC6H_SF16",
    "BC7_UNORM",
    "BC7_UNORM_SRGB",
)
