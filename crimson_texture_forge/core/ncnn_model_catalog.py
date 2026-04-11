from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence


@dataclass(frozen=True, slots=True)
class NcnnCatalogEntry:
    model_name: str
    native_scale: int
    usage_group: str
    content_type: str
    short_description: str
    source_name: str
    source_page_url: str
    model_files: Dict[str, str]


NCNN_CATALOG_SOURCE_LINKS: Sequence[tuple[str, str]] = (
    ("OpenModelDB", "https://openmodeldb.info/"),
    ("Upscayl Custom Models", "https://github.com/upscayl/custom-models"),
    ("Real-ESRGAN NCNN README", "https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan"),
)


def _upscayl_model_files(model_name: str) -> Dict[str, str]:
    base_url = "https://github.com/upscayl/custom-models/blob/main/models"
    return {
        f"{model_name}.param": f"{base_url}/{model_name}.param",
        f"{model_name}.bin": f"{base_url}/{model_name}.bin",
    }


NCNN_MODEL_CATALOG: Sequence[NcnnCatalogEntry] = (
    NcnnCatalogEntry(
        model_name="RealESRGAN_General_x4_v3",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, mixed quality input",
        short_description="Lightweight general 4x model and the safest first test for mixed visible textures.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("RealESRGAN_General_x4_v3"),
    ),
    NcnnCatalogEntry(
        model_name="RealESRGAN_General_WDN_x4_v3",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, BC1-like or noisy compressed input",
        short_description="General 4x WDN variant that is usually more tolerant of blockiness, ringing, and compression noise.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("RealESRGAN_General_WDN_x4_v3"),
    ),
    NcnnCatalogEntry(
        model_name="4x_NMKD-Siax_200k",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, clean to lightly compressed input",
        short_description="Popular universal model for cleaner visible textures that should stay natural rather than over-sharpened.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4x_NMKD-Siax_200k"),
    ),
    NcnnCatalogEntry(
        model_name="4xLSDIR",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, restoration-focused cleanup",
        short_description="LSDIR restoration model for visible textures that need more cleanup than a simple general-purpose pass.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4xLSDIR"),
    ),
    NcnnCatalogEntry(
        model_name="4xLSDIRplusC",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, stronger cleanup for rougher BC1-like input",
        short_description="LSDIR variant aimed at heavier cleanup when visible textures are rough, noisy, or more obviously compressed.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4xLSDIRplusC"),
    ),
    NcnnCatalogEntry(
        model_name="4xLSDIRCompactC3",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, faster cleanup model",
        short_description="Compact LSDIR-style model when you want a faster restoration-oriented pass on visible color textures.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4xLSDIRCompactC3"),
    ),
    NcnnCatalogEntry(
        model_name="uniscale_restore",
        native_scale=4,
        usage_group="Color / albedo textures - general / restoration",
        content_type="Visible color/albedo textures, mixed restoration use",
        short_description="General restoration-oriented model for visible textures when standard general models are not recovering enough structure.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("uniscale_restore"),
    ),
    NcnnCatalogEntry(
        model_name="4x_NMKD-Superscale-SP_178000_G",
        native_scale=4,
        usage_group="Color / albedo textures - clean input / sharper detail",
        content_type="Visible color/albedo textures, very clean BC7-like or artifact-light input",
        short_description="Stronger detail-recovery model for very clean visible textures where a sharper upscale is wanted.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4x_NMKD-Superscale-SP_178000_G"),
    ),
    NcnnCatalogEntry(
        model_name="4xNomos8kSC",
        native_scale=4,
        usage_group="Color / albedo textures - clean input / sharper detail",
        content_type="Visible color/albedo textures, crisp detail-forward output",
        short_description="Sharper visible-texture model when you want a more detail-forward result than the softer general models.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4xNomos8kSC"),
    ),
    NcnnCatalogEntry(
        model_name="4xHFA2k",
        native_scale=4,
        usage_group="Color / albedo textures - clean input / sharper detail",
        content_type="Visible color/albedo textures, sharper restoration output",
        short_description="High-frequency-emphasis model for visible textures when you want a stronger sharpened restoration look.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("4xHFA2k"),
    ),
    NcnnCatalogEntry(
        model_name="realesr-animevideov3-x2",
        native_scale=2,
        usage_group="Stylized color textures / UI / line art",
        content_type="Stylized color textures, UI art, line art, or cel-shaded input",
        short_description="Anime-focused 2x model for crisp stylized art and cleaner UI-like textures.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("realesr-animevideov3-x2"),
    ),
    NcnnCatalogEntry(
        model_name="realesr-animevideov3-x3",
        native_scale=3,
        usage_group="Stylized color textures / UI / line art",
        content_type="Stylized color textures, UI art, line art, or cel-shaded input",
        short_description="Anime-focused 3x model for stylized textures and non-photographic image content.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("realesr-animevideov3-x3"),
    ),
    NcnnCatalogEntry(
        model_name="realesr-animevideov3-x4",
        native_scale=4,
        usage_group="Stylized color textures / UI / line art",
        content_type="Stylized color textures, UI art, line art, or cel-shaded input",
        short_description="Anime-focused 4x model and the usual starting point for stylized textures, illustrations, and clean line work.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("realesr-animevideov3-x4"),
    ),
    NcnnCatalogEntry(
        model_name="unknown-2.0.1",
        native_scale=4,
        usage_group="Visible color textures - experimental / niche",
        content_type="Experimental visible-texture testing",
        short_description="Legacy experimental model kept for users who want to compare it against the more established general models.",
        source_name="Upscayl Custom Models",
        source_page_url="https://github.com/upscayl/custom-models",
        model_files=_upscayl_model_files("unknown-2.0.1"),
    ),
)


def get_ncnn_catalog_entry(model_name: str) -> Optional[NcnnCatalogEntry]:
    lookup = (model_name or "").strip()
    if not lookup:
        return None
    for entry in NCNN_MODEL_CATALOG:
        if entry.model_name == lookup:
            return entry
    return None
