from pathlib import Path
import tempfile
import unittest

from cdmw.core.archive_modding import MeshImportPreviewResult, MeshImportSupplementalFileSpec
from cdmw.core.final_package_preview import (
    FINAL_PREVIEW_BINDING_BASENAME_DIAGNOSTIC,
    FINAL_PREVIEW_BINDING_GENERATED,
    FINAL_PREVIEW_BINDING_ORIGINAL,
    FINAL_PREVIEW_MISSING_DDS,
    FINAL_PREVIEW_READY,
    FINAL_PREVIEW_SUPPORT_MAPS_ONLY,
    TEXTURE_PLAN_STATUS_LIKELY_GREY,
    TEXTURE_PLAN_STATUS_REVIEW,
    TEXTURE_PLAN_STATUS_READY,
    TEXTURE_PLAN_STATUS_SUPPORT_ONLY,
    build_dds_override_table_row,
    build_final_package_preview,
    build_replacement_texture_plan_rows,
    simplified_part_label,
    texture_plan_control_description,
)
from cdmw.core.mod_package import ModPackageExportOptions
from cdmw.models import ModelPreviewData, ModelPreviewMesh
from cdmw.modding.material_replacer import ReplacementTextureSet, ReplacementTextureSlot
from cdmw.modding.mesh_parser import ParsedMesh


def _preview(material_name: str = "Blade", texture_path: str = "source_preview.png") -> MeshImportPreviewResult:
    mesh = ModelPreviewMesh(
        material_name=material_name,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        texture_coordinates=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
        indices=[0, 1, 2],
        preview_texture_path=texture_path,
    )
    return MeshImportPreviewResult(
        rebuilt_data=b"not a parsed mesh in this focused test",
        parsed_mesh=ParsedMesh(path="character/model/test_weapon.pac", format="pac"),
        preview_model=ModelPreviewData(path="character/model/test_weapon.pac", meshes=[mesh]),
        summary_lines=[],
    )


def _sidecar(texture_path: str, parameter: str = "_overlayColorTexture", material: str = "Blade") -> bytes:
    return (
        f'<Root><SkinnedMeshMaterialWrapper _subMeshName="{material}">'
        f'<MaterialParameterTexture _name="{parameter}">'
        f'<ResourceReferencePath_ITexture _path="{texture_path}"/>'
        f"</MaterialParameterTexture>"
        f"</SkinnedMeshMaterialWrapper></Root>"
    ).encode("utf-8")


class FinalPackagePreviewTests(unittest.TestCase):
    def test_generated_sidecar_resolves_generated_dds_and_binds_base_texture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "blade.dds",
                    target_path="character/texture/blade_base.dds",
                    kind="texture_generated",
                    payload_data=b"DDS generated",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/blade_base.dds"),
                ),
            )

            result = build_final_package_preview(preview, supplemental_file_specs=specs)

            self.assertEqual([], result.likely_grey_materials)
            self.assertEqual(FINAL_PREVIEW_READY, result.binding_rows[0].status)
            self.assertEqual(FINAL_PREVIEW_BINDING_GENERATED, result.binding_rows[0].binding_source)
            self.assertIn("blade_base", result.preview_model.meshes[0].preview_texture_path)
            self.assertNotEqual("source_preview.png", result.preview_model.meshes[0].preview_texture_path)

    def test_generated_dds_exact_path_wins_over_original_dds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original_blade.dds"
            original.write_bytes(b"DDS original")
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "generated.dds",
                    target_path="character/texture/blade_base.dds",
                    kind="texture_generated",
                    payload_data=b"DDS generated",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/blade_base.dds"),
                ),
            )

            result = build_final_package_preview(
                preview,
                supplemental_file_specs=specs,
                original_dds_resolver=lambda _path: original,
            )

            self.assertEqual(FINAL_PREVIEW_READY, result.binding_rows[0].status)
            self.assertEqual(FINAL_PREVIEW_BINDING_GENERATED, result.binding_rows[0].binding_source)
            self.assertIn("blade_base", result.preview_model.meshes[0].preview_texture_path)

    def test_original_archive_dds_exact_path_is_ready_without_generated_dds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "blade_base.dds"
            original.write_bytes(b"DDS original")
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/blade_base.dds"),
                ),
            )

            result = build_final_package_preview(
                preview,
                supplemental_file_specs=specs,
                original_dds_resolver=lambda path: original if path == "character/texture/blade_base.dds" else None,
            )

            self.assertEqual(FINAL_PREVIEW_READY, result.binding_rows[0].status)
            self.assertEqual(FINAL_PREVIEW_BINDING_ORIGINAL, result.binding_rows[0].binding_source)
            self.assertEqual([], result.likely_grey_materials)
            self.assertIn("blade_base.dds", result.preview_model.meshes[0].preview_texture_path)

    def test_basename_fallback_is_diagnostic_not_exact_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "blade_base.dds"
            original.write_bytes(b"DDS original")
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/folder/blade_base.dds"),
                ),
            )

            result = build_final_package_preview(
                preview,
                supplemental_file_specs=specs,
                original_dds_resolver=lambda _path: None,
                original_dds_basename_resolver=lambda basename: (original,) if basename == "blade_base.dds" else (),
            )

            self.assertEqual(FINAL_PREVIEW_MISSING_DDS, result.binding_rows[0].status)
            self.assertEqual(FINAL_PREVIEW_BINDING_BASENAME_DIAGNOSTIC, result.binding_rows[0].binding_source)
            self.assertEqual("basename", result.binding_rows[0].confidence)
            self.assertIn("Blade", result.likely_grey_materials)

    def test_sidecar_missing_generated_dds_reports_missing_dds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/missing_base.dds"),
                ),
            )

            result = build_final_package_preview(preview, supplemental_file_specs=specs)

            self.assertEqual(FINAL_PREVIEW_MISSING_DDS, result.binding_rows[0].status)
            self.assertIn("Blade", result.likely_grey_materials)
            self.assertIn("character/texture/missing_base.dds", result.missing_texture_paths)

    def test_normal_and_height_only_reports_likely_grey(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "normal.dds",
                    target_path="character/texture/blade_n.dds",
                    kind="texture_generated",
                    payload_data=b"DDS normal",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "height.dds",
                    target_path="character/texture/blade_h.dds",
                    kind="texture_generated",
                    payload_data=b"DDS height",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=(
                        _sidecar("character/texture/blade_n.dds", "_normalTexture")
                        + _sidecar("character/texture/blade_h.dds", "_heightTexture")
                    ),
                ),
            )

            result = build_final_package_preview(preview, supplemental_file_specs=specs)

            self.assertIn("Blade", result.likely_grey_materials)
            self.assertEqual(FINAL_PREVIEW_SUPPORT_MAPS_ONLY, result.material_statuses[0].status)

    def test_material_sidecar_parameter_sets_material_semantics_for_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "mask.dds",
                    target_path="character/texture/blade_mask.dds",
                    kind="texture_generated",
                    payload_data=b"DDS mask",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/blade_mask.dds", "_roughnessTexture"),
                ),
            )

            result = build_final_package_preview(preview, supplemental_file_specs=specs)

            self.assertEqual("roughness", result.preview_model.meshes[0].preview_material_texture_subtype)
            self.assertIn("roughness", result.preview_model.meshes[0].preview_material_texture_packed_channels)

    def test_generated_sidecar_wins_over_source_preview_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview(texture_path="old/source/preview.png")
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "new.dds",
                    target_path="character/texture/new_base.dds",
                    kind="texture_generated",
                    payload_data=b"DDS new",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/new_base.dds"),
                ),
            )

            result = build_final_package_preview(preview, supplemental_file_specs=specs)

            self.assertNotIn("old/source/preview.png", result.preview_model.meshes[0].preview_texture_path)
            self.assertIn("new_base", result.preview_model.meshes[0].preview_texture_path)

    def test_custom_compact_paths_resolve_final_texture_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = _preview()
            specs = (
                MeshImportSupplementalFileSpec(
                    source_path=root / "compact.dds",
                    target_path="character/texture/compact_base.dds",
                    kind="texture_generated",
                    payload_data=b"DDS compact",
                ),
                MeshImportSupplementalFileSpec(
                    source_path=root / "test_weapon.pac_xml",
                    target_path="character/modelproperty/folder/test_weapon.pac_xml",
                    kind="sidecar_generated",
                    payload_data=_sidecar("character/texture/compact_base.dds"),
                ),
            )

            result = build_final_package_preview(
                preview,
                supplemental_file_specs=specs,
                export_options=ModPackageExportOptions(structure="custom_compact_paths"),
            )

            self.assertEqual("character/texture/compact_base.dds", result.binding_rows[0].resolved_texture_path)
            self.assertEqual("character/test_weapon.pac_xml", result.binding_rows[0].sidecar_path)
            self.assertEqual([], result.likely_grey_materials)


class TexturePlanHelperTests(unittest.TestCase):
    def test_part_label_simplifies_common_weapon_parts(self) -> None:
        self.assertEqual("Handle", simplified_part_label("CD_PHM_01_Dagger_Handle_0078"))
        self.assertEqual("Blade", simplified_part_label("cd_phm_01_dagger_blade_0078"))
        self.assertEqual("Guard", simplified_part_label("CD_PHM_01_Dagger_Guard_0078"))
        self.assertEqual("Part 3", simplified_part_label("CD_PHM_01_0078", fallback_index=3))

    def test_part_label_simplifies_non_weapon_parts(self) -> None:
        cases = {
            "CD_ARMOR_01_Helm_0001": "Helmet",
            "npcBodyUpper_0042": "Body",
            "Monster_Rathalos_Wing_L_0003": "Wing",
            "Creature_TailSpike_A": "Spike",
            "Costume_Gauntlets_R": "Gauntlet",
            "Village_Door_Frame_A": "Door",
            "Forest_TreeBranch_02": "Tree",
        }

        for raw_name, expected in cases.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(expected, simplified_part_label(raw_name))

    def test_part_label_avoids_short_substring_false_positive(self) -> None:
        self.assertEqual("Armor", simplified_part_label("ArmorPart"))

    def test_dds_override_base_assignment_is_ready(self) -> None:
        row = build_dds_override_table_row(
            {
                "target_name": "BladeMat",
                "part_display": "Blade",
                "slot_kind": "base",
                "role_label": "Base / Color",
                "parameter_name": "BaseColorTexture",
                "target_path": "character/texture/blade_base.dds",
                "source_path": r"C:\tmp\Blade_BaseColor.png",
                "checked": True,
                "visualized": True,
            }
        )

        self.assertEqual(TEXTURE_PLAN_STATUS_READY, row.status.label)
        self.assertEqual("Blade / BladeMat", row.part_material)
        self.assertEqual("Blade", row.part_label)
        self.assertEqual("BaseColorTexture: blade_base.dds", row.original_slot)

    def test_dds_override_missing_base_is_likely_grey(self) -> None:
        row = build_dds_override_table_row(
            {
                "target_name": "BladeMat",
                "slot_kind": "base",
                "role_label": "Base / Color",
                "target_path": "character/texture/blade_base.dds",
                "checked": False,
                "visualized": True,
            }
        )

        self.assertEqual(TEXTURE_PLAN_STATUS_LIKELY_GREY, row.status.label)
        self.assertEqual("red", row.status.color_key)

    def test_dds_override_normal_and_height_are_support_only(self) -> None:
        for slot_kind in ("normal", "height"):
            row = build_dds_override_table_row(
                {
                    "target_name": "BladeMat",
                    "slot_kind": slot_kind,
                    "target_path": f"character/texture/blade_{slot_kind}.dds",
                    "source_path": f"/tmp/blade_{slot_kind}.png",
                    "checked": True,
                    "visualized": True,
                }
            )

            self.assertEqual(TEXTURE_PLAN_STATUS_SUPPORT_ONLY, row.status.label)

    def test_dds_override_standalone_pbr_maps_need_review(self) -> None:
        for slot_kind in ("metallic", "roughness", "ao"):
            row = build_dds_override_table_row(
                {
                    "target_name": "BladeMat",
                    "slot_kind": slot_kind,
                    "target_path": f"character/texture/blade_{slot_kind}.dds",
                    "source_path": f"/tmp/blade_{slot_kind}.png",
                    "checked": True,
                    "visualized": True,
                }
            )

            self.assertEqual(TEXTURE_PLAN_STATUS_REVIEW, row.status.label)
            self.assertIn("pack", row.controls.lower())

    def test_material_mask_description_mentions_shine_metal_roughness(self) -> None:
        description = texture_plan_control_description("material").lower()

        self.assertIn("shine", description)
        self.assertIn("metal", description)
        self.assertIn("roughness", description)

    def test_standalone_pbr_maps_are_detected_but_not_game_effective(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metallic = root / "Blade_Metallic.png"
            metallic.write_bytes(b"")
            texture_set = ReplacementTextureSet(
                material_name="Blade",
                slots={
                    "metallic": ReplacementTextureSlot("Blade", "metallic", metallic),
                },
            )

            rows = build_replacement_texture_plan_rows({"blade": texture_set})

            pbr_rows = [row for row in rows if row.slot_kind == "metallic"]
            self.assertEqual(1, len(pbr_rows))
            self.assertEqual(TEXTURE_PLAN_STATUS_REVIEW, pbr_rows[0].status.label)
            self.assertFalse(pbr_rows[0].game_effective)
            self.assertIn("pack", pbr_rows[0].controls.lower())

    def test_missing_base_color_creates_red_likely_grey_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normal = root / "Blade_Normal.png"
            normal.write_bytes(b"")
            texture_set = ReplacementTextureSet(
                material_name="Blade",
                slots={
                    "normal": ReplacementTextureSlot("Blade", "normal", normal),
                },
            )

            rows = build_replacement_texture_plan_rows({"blade": texture_set})

            missing_rows = [row for row in rows if row.source == "Missing"]
            self.assertEqual(1, len(missing_rows))
            self.assertEqual(TEXTURE_PLAN_STATUS_LIKELY_GREY, missing_rows[0].status.label)
            self.assertEqual("red", missing_rows[0].status.color_key)


if __name__ == "__main__":
    unittest.main()
