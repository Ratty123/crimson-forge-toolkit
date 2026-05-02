from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import unittest
from unittest.mock import patch

from cdmw.core.archive import (
    _ArchiveModelSidecarTextureBinding,
    _attach_model_sidecar_texture_preview_paths,
    _attach_model_texture_preview_paths,
    _attach_model_support_texture_preview_paths,
    _iter_model_sidecar_binding_submesh_keys,
    normalize_texture_reference_for_sidecar_lookup,
)
from cdmw.models import ArchiveEntry, ModelPreviewData, ModelPreviewMesh


def _entry(path: str) -> ArchiveEntry:
    return ArchiveEntry(
        path=path,
        pamt_path=Path("0000/0.pamt"),
        paz_file=Path("0000/1.paz"),
        offset=0,
        comp_size=1,
        orig_size=1,
        flags=0,
        paz_index=0,
    )


def _texture_maps(*paths: str):
    by_normalized = defaultdict(list)
    by_basename = defaultdict(list)
    for path in paths:
        entry = _entry(path)
        by_normalized[normalize_texture_reference_for_sidecar_lookup(path)].append(entry)
        by_basename[Path(path).name.lower()].append(entry)
    return by_normalized, by_basename


class ArchivePreviewTextureBindingTests(unittest.TestCase):
    def test_sidecar_binding_keys_prefer_explicit_part_over_linked_model_path(self) -> None:
        binding = _ArchiveModelSidecarTextureBinding(
            texture_path="character/texture/part_b.dds",
            parameter_name="_baseColorTexture",
            submesh_name="CD_Test_Part_B",
            linked_mesh_path="character/model/cd_test_model.pac",
        )

        keys = _iter_model_sidecar_binding_submesh_keys(binding)

        self.assertIn("cdtestpartb", keys)
        self.assertNotIn("cdtestmodel", keys)

    def test_anonymous_meshes_use_ordered_sidecar_visible_bindings(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/part_a.dds",
            "character/texture/part_b.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[
                ModelPreviewMesh(material_name="unknown_10", texture_name="unknown_10"),
                ModelPreviewMesh(material_name="unknown_20", texture_name="unknown_20"),
            ],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/part_a.dds",
                parameter_name="_diffuseTextureR",
                submesh_name="CD_Test_Part_A",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/part_b.dds",
                parameter_name="_diffuseTextureR",
                submesh_name="CD_Test_Part_B",
                sidecar_kind="pac_xml",
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            lines = _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual("character/texture/part_a.dds", model.meshes[0].texture_name)
        self.assertEqual("character/texture/part_b.dds", model.meshes[1].texture_name)
        self.assertIn("ordered sidecar material wrapper", "\n".join(lines))

    def test_sidecar_overlay_base_promotes_material_tint_to_preview_color(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps("character/texture/cd_common_default_overlay_old.dds")
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Handle", texture_name="CD_Test_Handle")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_common_default_overlay_old.dds",
                parameter_name="_overlayColorTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
                tint_color=(0.62, 0.31, 0.14),
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="mesh_base_first",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual((0.62, 0.31, 0.14), model.meshes[0].preview_color)
        self.assertEqual("low_authority_overlay", model.meshes[0].preview_base_texture_quality)
        self.assertIn("low-detail overlay/default", model.meshes[0].preview_texture_approximation_note)

    def test_layer_visible_sidecar_texture_replaces_low_authority_overlay_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/cd_common_default_overlay_old.dds",
            "character/texture/cd_texturelayer_001_0001.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Handle", texture_name="CD_Test_Handle")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_common_default_overlay_old.dds",
                parameter_name="_overlayColorTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_texturelayer_001_0001.dds",
                parameter_name="_detailDiffuseMaskR",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="mesh_base_first",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )
            lines = _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
                fallback_only=True,
            )

        self.assertEqual("character/texture/cd_texturelayer_001_0001.dds", model.meshes[0].texture_name)
        self.assertEqual(
            "preview://character/texture/cd_texturelayer_001_0001.dds",
            model.meshes[0].preview_texture_path,
        )
        self.assertEqual("resolved_base", model.meshes[0].preview_base_texture_quality)
        self.assertIn("Promoted 1 sidecar visible layer texture preview", "\n".join(lines))

    def test_detail_layer_visible_texture_beats_grime_layer_for_promoted_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/cd_common_default_overlay_old.dds",
            "character/texture/cd_texturelayer_003_0102.dds",
            "character/texture/cd_texturelayer_001_0034.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Shield", texture_name="CD_Test_Shield")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_common_default_overlay_old.dds",
                parameter_name="_overlayColorTexture",
                submesh_name="CD_Test_Shield",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_texturelayer_003_0102.dds",
                parameter_name="_grimeDiffuseTextureR",
                submesh_name="CD_Test_Shield",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_texturelayer_001_0034.dds",
                parameter_name="_detailDiffuseMaskR",
                submesh_name="CD_Test_Shield",
                sidecar_kind="pac_xml",
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="mesh_base_first",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )
            lines = _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
                fallback_only=True,
            )

        self.assertEqual("character/texture/cd_texturelayer_001_0034.dds", model.meshes[0].texture_name)
        self.assertEqual(
            "preview://character/texture/cd_texturelayer_001_0034.dds",
            model.meshes[0].preview_texture_path,
        )
        self.assertIn("Promoted 1 sidecar visible layer texture preview", "\n".join(lines))

    def test_technical_sidecar_texture_does_not_replace_low_authority_overlay_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/cd_common_default_overlay_old.dds",
            "character/texture/cd_test_handle_n.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Handle", texture_name="CD_Test_Handle")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_common_default_overlay_old.dds",
                parameter_name="_overlayColorTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_test_handle_n.dds",
                parameter_name="_normalTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="mesh_base_first",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )
            lines = _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
                fallback_only=True,
            )

        self.assertEqual("character/texture/cd_common_default_overlay_old.dds", model.meshes[0].texture_name)
        self.assertEqual(
            "preview://character/texture/cd_common_default_overlay_old.dds",
            model.meshes[0].preview_texture_path,
        )
        self.assertEqual("low_authority_overlay", model.meshes[0].preview_base_texture_quality)
        self.assertNotIn("Promoted", "\n".join(lines))

    def test_placeholder_none_texture_does_not_become_visible_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps("texture/nonetexture0x00000000.dds")
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Handle", texture_name="CD_Test_Handle")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="texture/nonetexture0x00000000.dds",
                parameter_name="_diffuseTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
                tint_color=(0.5, 0.49, 0.48),
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            lines = _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual("", model.meshes[0].preview_texture_path)
        self.assertEqual("CD_Test_Handle", model.meshes[0].texture_name)
        self.assertEqual("material_color_fallback", model.meshes[0].preview_base_texture_quality)
        self.assertIn("material color fallback", "\n".join(lines))

    def test_placeholder_none_texture_is_skipped_when_promoting_layer_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/cd_common_default_overlay_old.dds",
            "texture/nonetexture0x00000000.dds",
            "character/texture/cd_texturelayer_003_0005.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Handle", texture_name="CD_Test_Handle")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_common_default_overlay_old.dds",
                parameter_name="_overlayColorTexture",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="texture/nonetexture0x00000000.dds",
                parameter_name="_grimeDiffuseTextureR",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/cd_texturelayer_003_0005.dds",
                parameter_name="_detailDiffuseMaskR",
                submesh_name="CD_Test_Handle",
                sidecar_kind="pac_xml",
            ),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="mesh_base_first",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )
            _attach_model_sidecar_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                visible_texture_mode="layer_aware_visible",
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
                fallback_only=True,
            )

        self.assertEqual("character/texture/cd_texturelayer_003_0005.dds", model.meshes[0].texture_name)
        self.assertEqual(
            "preview://character/texture/cd_texturelayer_003_0005.dds",
            model.meshes[0].preview_texture_path,
        )

    def test_sidecar_material_color_survives_missing_visible_dds(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps()
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="CD_Test_Blade", texture_name="CD_Test_Blade")],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding(
                texture_path="character/texture/missing_base.dds",
                parameter_name="_baseColorTexture",
                submesh_name="CD_Test_Blade",
                sidecar_kind="pac_xml",
                tint_color=(0.22, 0.26, 0.42),
            ),
        )

        lines = _attach_model_sidecar_texture_preview_paths(
            Path("texconv.exe"),
            source_entry,
            model,
            parsed_mesh=None,
            sidecar_texture_bindings=bindings,
            visible_texture_mode="mesh_base_first",
            texture_entries_by_normalized_path=by_normalized,
            texture_entries_by_basename=by_basename,
        )

        self.assertEqual((0.22, 0.26, 0.42), model.meshes[0].preview_color)
        self.assertEqual("", model.meshes[0].preview_texture_path)
        self.assertEqual("material_color_fallback", model.meshes[0].preview_base_texture_quality)
        self.assertIn("material color fallback", "\n".join(lines))

    def test_material_name_base_fallback_uses_visible_sibling_dds(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps(
            "character/texture/part_a_d.dds",
            "character/texture/part_a_n.dds",
        )
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="part_a", texture_name="part_a")],
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual("character/texture/part_a_d.dds", model.meshes[0].texture_name)
        self.assertEqual("preview://character/texture/part_a_d.dds", model.meshes[0].preview_texture_path)

    def test_technical_sibling_dds_is_not_promoted_to_visible_base(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        by_normalized, by_basename = _texture_maps("character/texture/part_a_n.dds")
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[ModelPreviewMesh(material_name="part_a", texture_name="part_a")],
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            _attach_model_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual("part_a", model.meshes[0].texture_name)
        self.assertEqual("", model.meshes[0].preview_texture_path)

    def test_anonymous_meshes_use_ordered_sidecar_support_bindings(self) -> None:
        source_entry = _entry("character/model/cd_test_model.pac")
        texture_paths = (
            "character/texture/part_a_n.dds",
            "character/texture/part_b_n.dds",
            "character/texture/part_a_ma.dds",
            "character/texture/part_b_ma.dds",
            "character/texture/part_a_disp.dds",
            "character/texture/part_b_disp.dds",
        )
        by_normalized, by_basename = _texture_maps(*texture_paths)
        model = ModelPreviewData(
            path=source_entry.path,
            meshes=[
                ModelPreviewMesh(material_name="unknown_10", texture_name="unknown_10"),
                ModelPreviewMesh(material_name="unknown_20", texture_name="unknown_20"),
            ],
        )
        bindings = (
            _ArchiveModelSidecarTextureBinding("character/texture/part_a_n.dds", "_normalTexture", "Part_A"),
            _ArchiveModelSidecarTextureBinding("character/texture/part_b_n.dds", "_normalTexture", "Part_B"),
            _ArchiveModelSidecarTextureBinding("character/texture/part_a_ma.dds", "_materialTexture", "Part_A"),
            _ArchiveModelSidecarTextureBinding("character/texture/part_b_ma.dds", "_materialTexture", "Part_B"),
            _ArchiveModelSidecarTextureBinding("character/texture/part_a_disp.dds", "_heightTexture", "Part_A"),
            _ArchiveModelSidecarTextureBinding("character/texture/part_b_disp.dds", "_heightTexture", "Part_B"),
        )

        with patch(
            "cdmw.core.archive._ensure_archive_model_texture_preview_path",
            side_effect=lambda _texconv, texture_entry, **_kwargs: f"preview://{texture_entry.path}",
        ):
            lines = _attach_model_support_texture_preview_paths(
                Path("texconv.exe"),
                source_entry,
                model,
                parsed_mesh=None,
                sidecar_texture_bindings=bindings,
                texture_entries_by_normalized_path=by_normalized,
                texture_entries_by_basename=by_basename,
            )

        self.assertEqual("character/texture/part_a_n.dds", model.meshes[0].preview_normal_texture_name)
        self.assertEqual("character/texture/part_b_n.dds", model.meshes[1].preview_normal_texture_name)
        self.assertEqual("character/texture/part_a_ma.dds", model.meshes[0].preview_material_texture_name)
        self.assertEqual("character/texture/part_b_disp.dds", model.meshes[1].preview_height_texture_name)
        self.assertIn("anonymous support-map", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
