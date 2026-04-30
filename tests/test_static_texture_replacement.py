from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cdmw.core.archive_modding import (
    ArchivePatchRequest,
    MeshImportPreviewResult,
    MeshImportSupplementalFileSpec,
    export_archive_mesh_payloads_to_mod_ready_loose,
)
from cdmw.core.mod_package import ModPackageExportOptions
from cdmw.core.pipeline import parse_dds
from cdmw.models import ArchiveEntry, ArchiveModelTextureReference, ModelPreviewData, ModPackageInfo
from cdmw.modding.material_replacer import (
    ReplacementTextureSlot,
    TextureReplacementReport,
    _choose_source_materials_for_targets,
    build_texture_replacement_payloads,
    _build_texture_payload,
    classify_texture_assignment_guidance,
    group_replacement_texture_sets,
    is_shared_material_layer_texture,
)
from cdmw.modding.mesh_parser import ParsedMesh, SubMesh
from cdmw.modding.static_mesh_replacer import StaticSubmeshMapping, StaticTextureSlotOverride


def _entry(path: str, root: Path) -> ArchiveEntry:
    package_root = root / "0009"
    package_root.mkdir(parents=True, exist_ok=True)
    return ArchiveEntry(
        path=path,
        pamt_path=package_root / "package.pamt",
        paz_file=package_root / "package.paz",
        offset=0,
        comp_size=0,
        orig_size=0,
        flags=0,
        paz_index=0,
    )


def _write_fake_png_header(path: Path, width: int, height: int) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def _fake_dds_bytes(width: int, height: int, *, mips: int = 1, fourcc: bytes = b"DXT1") -> bytes:
    data = bytearray(128)
    data[0:4] = b"DDS "
    struct.pack_into("<I", data, 4 + 0, 124)
    struct.pack_into("<I", data, 4 + 8, height)
    struct.pack_into("<I", data, 4 + 12, width)
    struct.pack_into("<I", data, 4 + 24, mips)
    struct.pack_into("<I", data, 4 + 72, 32)
    struct.pack_into("<I", data, 4 + 76, 0x4)
    data[4 + 80 : 4 + 84] = fourcc
    return bytes(data)


class StaticTextureReplacementTests(unittest.TestCase):
    def test_png_to_dds_defaults_to_source_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_png = root / "replacement_Base_Color.png"
            original_dds = root / "original.dds"
            texconv = root / "texconv.exe"
            _write_fake_png_header(source_png, 4096, 4096)
            original_dds.write_bytes(_fake_dds_bytes(256, 512, mips=10))
            texconv.write_bytes(b"fake")

            def fake_texconv(command: list[str], **_kwargs: object) -> tuple[int, str, str]:
                out_dir = Path(command[command.index("-o") + 1])
                width = int(command[command.index("-w") + 1])
                height = int(command[command.index("-h") + 1])
                mips = int(command[command.index("-m") + 1])
                produced = out_dir / f"{Path(command[-1]).stem}.dds"
                produced.write_bytes(_fake_dds_bytes(width, height, mips=mips))
                return 0, "", ""

            with patch("cdmw.core.common.run_process_with_cancellation", side_effect=fake_texconv):
                payload = _build_texture_payload(
                    ReplacementTextureSlot("replacement", "base", source_png),
                    target_entry=object(),
                    texconv_path=texconv,
                    read_original_texture_bytes=lambda _entry: original_dds.read_bytes(),
                    original_texture_source_path=lambda _entry: original_dds,
                    report=TextureReplacementReport(),
                    on_log=None,
                )

            output_dds = root / "output.dds"
            output_dds.write_bytes(payload)
            info = parse_dds(output_dds)
            self.assertEqual((4096, 4096), (info.width, info.height))
            self.assertEqual(13, info.mip_count)

    def test_png_to_dds_can_match_original_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_png = root / "replacement_Base_Color.png"
            original_dds = root / "original.dds"
            texconv = root / "texconv.exe"
            _write_fake_png_header(source_png, 4096, 4096)
            original_dds.write_bytes(_fake_dds_bytes(256, 512, mips=10))
            texconv.write_bytes(b"fake")
            report = TextureReplacementReport()

            def fake_texconv(command: list[str], **_kwargs: object) -> tuple[int, str, str]:
                out_dir = Path(command[command.index("-o") + 1])
                width = int(command[command.index("-w") + 1])
                height = int(command[command.index("-h") + 1])
                mips = int(command[command.index("-m") + 1])
                produced = out_dir / f"{Path(command[-1]).stem}.dds"
                produced.write_bytes(_fake_dds_bytes(width, height, mips=mips))
                return 0, "", ""

            with patch("cdmw.core.common.run_process_with_cancellation", side_effect=fake_texconv):
                payload = _build_texture_payload(
                    ReplacementTextureSlot("replacement", "base", source_png),
                    target_entry=object(),
                    texconv_path=texconv,
                    read_original_texture_bytes=lambda _entry: original_dds.read_bytes(),
                    original_texture_source_path=lambda _entry: original_dds,
                    report=report,
                    on_log=None,
                    texture_output_size_mode="original",
                )

            output_dds = root / "output.dds"
            output_dds.write_bytes(payload)
            info = parse_dds(output_dds)
            self.assertEqual((256, 512), (info.width, info.height))
            self.assertEqual(10, info.mip_count)
            self.assertTrue(any("smaller than source" in warning for warning in report.warnings))

    def test_shared_texture_layers_are_identified_as_optional(self) -> None:
        self.assertTrue(is_shared_material_layer_texture("character/texture/cd_texturelayer_003_0101.dds"))
        self.assertTrue(is_shared_material_layer_texture("character/texture/cd_temp_r_m.dds"))
        self.assertTrue(is_shared_material_layer_texture("character/texture/cd_metal_05.dds"))
        self.assertFalse(is_shared_material_layer_texture("character/texture/cd_phm_01_sword_blade_0278_o.dds"))

    def test_texture_assignment_guidance_is_conservative(self) -> None:
        direct = classify_texture_assignment_guidance(
            "_normalTexture",
            "character/texture/cd_phm_01_sword_blade_0278_n.dds",
            suggested_source=r"C:\tmp\Blade_Normal.png",
        )
        self.assertTrue(direct.checked_by_default)
        self.assertEqual(direct.confidence, "high")

        shared = classify_texture_assignment_guidance(
            "_detailTexture",
            "character/texture/cd_texturelayer_003_0101.dds",
            suggested_source=r"C:\tmp\detail.png",
        )
        self.assertFalse(shared.checked_by_default)
        self.assertTrue(shared.advanced)
        self.assertIn("shared", shared.state_label.lower())

        shared_metal = classify_texture_assignment_guidance(
            "_grimeDiffuseTextureG",
            "character/texture/cd_metal_05.dds",
            suggested_source=r"C:\tmp\Blade_albedo.png",
        )
        self.assertFalse(shared_metal.checked_by_default)
        self.assertTrue(shared_metal.advanced)
        self.assertIn("shared", shared_metal.state_label.lower())

        emissive = classify_texture_assignment_guidance(
            "_emissiveIntensityTexture",
            "character/texture/cd_phm_02_blade_0014_emi.dds",
            suggested_source=r"C:\tmp\Blade_albedo.png",
        )
        self.assertFalse(emissive.checked_by_default)
        self.assertTrue(emissive.advanced)

        color_blend = classify_texture_assignment_guidance(
            "_colorBlendingMaskTexture",
            "character/texture/cd_phm_01_sword_handle_0278_ma.dds",
            suggested_source=r"C:\tmp\handle_mask.png",
        )
        self.assertFalse(color_blend.checked_by_default)
        self.assertTrue(color_blend.advanced)

        repeated = classify_texture_assignment_guidance(
            "_baseColorTexture",
            "character/texture/cd_phm_01_sword_handle_0278_o.dds",
            suggested_source=r"C:\tmp\handle_BaseColor.png",
            repeated_suggestion_count=3,
        )
        self.assertFalse(repeated.checked_by_default)
        self.assertTrue(repeated.advanced)
        self.assertIn("repeated", repeated.state_label.lower())

    def test_texture_sets_can_match_part_named_files_when_obj_material_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blade_base = root / "blade.001_Base_Color.png"
            blade_normal = root / "blade.001_Normal_OpenGL.png"
            handle_base = root / "Handle.002_Base_Color.png"
            for path in (blade_base, blade_normal, handle_base):
                path.write_bytes(b"")

            obj_mesh = ParsedMesh(
                path="Rathalos_Sword_Final.obj",
                format="obj",
                submeshes=[
                    SubMesh(
                        name="Sword_Body_low_Cube.002",
                        material="Rathalos.001",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    ),
                    SubMesh(
                        name="Sword_Handle_low_Cube.004",
                        material="Handle.002",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    ),
                ],
            )
            texture_sets = group_replacement_texture_sets(
                (blade_base, blade_normal, handle_base),
                obj_mesh=obj_mesh,
            )
            chosen = _choose_source_materials_for_targets(
                obj_mesh,
                texture_sets,
                (
                    StaticSubmeshMapping(
                        target_submesh_index=0,
                        target_submesh_name="CD_PHM_01_Dagger_Blade_0078",
                        source_submesh_indices=[0],
                        target_material_slot_index=0,
                    ),
                    StaticSubmeshMapping(
                        target_submesh_index=1,
                        target_submesh_name="CD_PHM_01_Dagger_Handle_0078",
                        source_submesh_indices=[1],
                        target_material_slot_index=1,
                    ),
                ),
                TextureReplacementReport(),
            )

            self.assertEqual("blade.001", chosen["cd_phm_01_dagger_blade_0078"])
            self.assertEqual("Handle.002", chosen["cd_phm_01_dagger_handle_0078"])

    def test_texture_sets_detect_single_material_files_without_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture_files = (
                root / "Base Color.png",
                root / "Normal OpenGL.png",
                root / "Height.png",
                root / "Mixed AO.png",
            )
            for path in texture_files:
                path.write_bytes(b"")

            obj_mesh = ParsedMesh(
                path="single.obj",
                format="obj",
                submeshes=[
                    SubMesh(
                        name="Blade.001",
                        material="HeroBlade",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ],
            )

            texture_sets = group_replacement_texture_sets(texture_files, obj_mesh=obj_mesh)
            self.assertIn("heroblade", texture_sets)
            slots = texture_sets["heroblade"].slots
            self.assertEqual("Base Color.png", slots["base"].source_path.name)
            self.assertEqual("Normal OpenGL.png", slots["normal"].source_path.name)
            self.assertEqual("opengl", slots["normal"].normal_space)
            self.assertEqual("Height.png", slots["height"].source_path.name)
            self.assertEqual("Mixed AO.png", slots["ao"].source_path.name)

    def test_texture_sets_accept_space_separated_material_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "Handle.002 Base Color.png"
            normal = root / "Handle.002 Normal OpenGL.png"
            material = root / "Handle.002 Material Mask.png"
            for path in (base, normal, material):
                path.write_bytes(b"")

            obj_mesh = ParsedMesh(
                path="handle.obj",
                format="obj",
                submeshes=[
                    SubMesh(
                        name="Handle.002",
                        material="Handle.002",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ],
            )

            texture_sets = group_replacement_texture_sets((base, normal, material), obj_mesh=obj_mesh)
            self.assertIn("handle.002", texture_sets)
            slots = texture_sets["handle.002"].slots
            self.assertEqual("Handle.002 Base Color.png", slots["base"].source_path.name)
            self.assertEqual("Handle.002 Normal OpenGL.png", slots["normal"].source_path.name)
            self.assertEqual("Handle.002 Material Mask.png", slots["material"].source_path.name)

    def test_texture_sets_detect_gltf_metallic_roughness_as_packed_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "20_-_Default_baseColor.png"
            normal = root / "20_-_Default_normal.png"
            material = root / "20_-_Default_metallicRoughness.png"
            for path in (base, normal, material):
                path.write_bytes(b"")

            texture_sets = group_replacement_texture_sets((base, normal, material))
            self.assertIn("20_-_default", texture_sets)
            slots = texture_sets["20_-_default"].slots
            self.assertEqual("20_-_Default_baseColor.png", slots["base"].source_path.name)
            self.assertEqual("20_-_Default_normal.png", slots["normal"].source_path.name)
            self.assertEqual("20_-_Default_metallicRoughness.png", slots["material"].source_path.name)
            self.assertNotIn("metallic", slots)
            self.assertNotIn("roughness", slots)

    def test_texture_sets_prefer_base_color_over_emissive_for_base_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            emissive = root / "New_Sword_lp_UV_Emissive.png"
            base = root / "New_Sword_lp_UV_BaseColor.png"
            for path in (emissive, base):
                path.write_bytes(b"")

            texture_sets = group_replacement_texture_sets((emissive, base))
            self.assertIn("new_sword_lp_uv", texture_sets)
            slots = texture_sets["new_sword_lp_uv"].slots
            self.assertEqual("New_Sword_lp_UV_BaseColor.png", slots["base"].source_path.name)

    def test_mesh_loose_export_includes_generated_payloads_but_not_unselected_related_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = _entry("character/model/weapon/test_weapon.pac", root)
            pab = _entry("character/model/test_skeleton.pab", root)
            preview = MeshImportPreviewResult(
                rebuilt_data=b"rebuilt",
                parsed_mesh=ParsedMesh(path=primary.path, format="pac"),
                preview_model=ModelPreviewData(),
                summary_lines=[],
                texture_references=(
                    ArchiveModelTextureReference(
                        reference_name=pab.basename,
                        resolved_archive_path=pab.path,
                        resolved_entry=pab,
                        reference_kind="skeleton",
                        relation_group="Skeleton / Rig",
                    ),
                ),
                supplemental_file_specs=(
                    MeshImportSupplementalFileSpec(
                        source_path=root / "generated.dds",
                        target_path="character/texture/generated.dds",
                        kind="texture_generated",
                        payload_data=b"DDS generated",
                    ),
                    MeshImportSupplementalFileSpec(
                        source_path=root / "generated.pac_xml",
                        target_path="character/modelproperty/test_weapon.pac_xml",
                        kind="sidecar_generated",
                        payload_data=b"<Material />",
                    ),
                ),
            )

            result = export_archive_mesh_payloads_to_mod_ready_loose(
                (ArchivePatchRequest(primary, b"rebuilt"),),
                primary_entry=primary,
                preview_result=preview,
                source_obj_path=root / "source.obj",
                parent_root=root,
                package_info=ModPackageInfo(title="Mesh Mod"),
                related_entries_to_include=(),
                supplemental_files_to_include=preview.supplemental_file_specs,
            )

            self.assertTrue((result.package_root / "character" / "texture" / "generated.dds").exists())
            self.assertTrue((result.package_root / "character" / "modelproperty" / "test_weapon.pac_xml").exists())
            self.assertFalse((result.package_root / "character" / "model" / "test_skeleton.pab").exists())
            manifest = json.loads((result.package_root / "manifest.json").read_text(encoding="utf-8"))
            files = {item["path"]: item for item in manifest["files"]}
            self.assertIn("Generated replacement texture", files["character/texture/generated.dds"]["note"])
            self.assertIn("Generated patched sidecar", files["character/modelproperty/test_weapon.pac_xml"]["note"])

    def test_mesh_loose_export_custom_compact_paths_keeps_textures_under_character_texture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = _entry(
                "character/model/1_pc/1_phm/weapon/1_onehandweapon/cd_phm_01_sword_0278.pac",
                root,
            )
            sidecar_path = "character/modelproperty/1_pc/1_phm/weapon/1_onehandweapon/cd_phm_01_sword_0278.pac_xml"
            preview = MeshImportPreviewResult(
                rebuilt_data=b"rebuilt",
                parsed_mesh=ParsedMesh(path=primary.path, format="pac"),
                preview_model=ModelPreviewData(),
                summary_lines=[],
                supplemental_file_specs=(
                    MeshImportSupplementalFileSpec(
                        source_path=root / "generated.dds",
                        target_path="character/texture/cd_phm_01_sword_0278_blade_base.dds",
                        kind="texture_generated",
                        payload_data=b"DDS generated",
                    ),
                    MeshImportSupplementalFileSpec(
                        source_path=root / "generated.pac_xml",
                        target_path=sidecar_path,
                        kind="sidecar_generated",
                        payload_data=b"<Material />",
                    ),
                ),
            )

            result = export_archive_mesh_payloads_to_mod_ready_loose(
                (ArchivePatchRequest(primary, b"rebuilt"),),
                primary_entry=primary,
                preview_result=preview,
                source_obj_path=root / "source.obj",
                parent_root=root,
                package_info=ModPackageInfo(title="Compact Mesh Mod"),
                export_options=ModPackageExportOptions(structure="custom_compact_paths"),
                related_entries_to_include=(),
                supplemental_files_to_include=preview.supplemental_file_specs,
            )

            self.assertTrue((result.package_root / "files" / "character" / "cd_phm_01_sword_0278.pac").exists())
            self.assertTrue((result.package_root / "files" / "character" / "cd_phm_01_sword_0278.pac_xml").exists())
            self.assertTrue(
                (
                    result.package_root
                    / "files"
                    / "character"
                    / "texture"
                    / "cd_phm_01_sword_0278_blade_base.dds"
                ).exists()
            )
            self.assertFalse((result.package_root / "character").exists())
            self.assertFalse((result.package_root / "files" / "character" / "model").exists())
            self.assertFalse((result.package_root / "files" / "character" / "modelproperty").exists())

            manifest = json.loads((result.package_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("custom_compact_paths", manifest["structure"])
            self.assertEqual("files", manifest["files_root"])
            self.assertEqual("character/cd_phm_01_sword_0278.pac", manifest["assets"][0]["entry_path"])
            files = {item["path"]: item for item in manifest["files"]}
            self.assertIn("character/cd_phm_01_sword_0278.pac", files)
            self.assertIn("character/cd_phm_01_sword_0278.pac_xml", files)
            self.assertIn("character/texture/cd_phm_01_sword_0278_blade_base.dds", files)

    def test_mesh_loose_export_includes_explicitly_selected_related_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = _entry("character/model/weapon/test_weapon.pac", root)
            hkx = _entry("character/bin__/meshphysics/test_weapon.hkx", root)
            preview = MeshImportPreviewResult(
                rebuilt_data=b"rebuilt",
                parsed_mesh=ParsedMesh(path=primary.path, format="pac"),
                preview_model=ModelPreviewData(),
                summary_lines=[],
            )

            def fake_extract(entry: ArchiveEntry, target_path: Path, **_kwargs: object) -> Path:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(f"related:{entry.path}".encode("utf-8"))
                return target_path

            with patch("cdmw.core.archive.extract_archive_entry", side_effect=fake_extract):
                result = export_archive_mesh_payloads_to_mod_ready_loose(
                    (ArchivePatchRequest(primary, b"rebuilt"),),
                    primary_entry=primary,
                    preview_result=preview,
                    source_obj_path=root / "source.obj",
                    parent_root=root,
                    package_info=ModPackageInfo(title="Mesh Mod"),
                    related_entries_to_include=(hkx,),
                )

            self.assertTrue((result.package_root / "character" / "bin__" / "meshphysics" / "test_weapon.hkx").exists())
            manifest = json.loads((result.package_root / "manifest.json").read_text(encoding="utf-8"))
            files = {item["path"]: item for item in manifest["files"]}
            self.assertIn("Selected archive related file", files["character/bin__/meshphysics/test_weapon.hkx"]["note"])

    def test_pac_driven_sidecar_auto_injects_missing_active_base_texture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texconv = root / "texconv.exe"
            texconv.write_bytes(b"fake")
            handle_base = root / "Handle.002_BaseColor.png"
            handle_normal = root / "Handle.002_Normal.png"
            handle_metallic = root / "Handle.002_Metallic.png"
            handle_roughness = root / "Handle.002_Roughness.png"
            for source_texture in (handle_base, handle_normal, handle_metallic, handle_roughness):
                _write_fake_png_header(source_texture, 4096, 4096)
            template_base_dds = root / "template_base.dds"
            template_normal_dds = root / "template_normal.dds"
            template_material_dds = root / "template_material.dds"
            template_base_dds.write_bytes(_fake_dds_bytes(512, 512, mips=10))
            template_normal_dds.write_bytes(_fake_dds_bytes(512, 512, mips=10))
            template_material_dds.write_bytes(_fake_dds_bytes(512, 512, mips=10))
            base_entry = _entry("character/texture/cd_phm_01_sword_blade_0278_o.dds", root)
            normal_entry = _entry("character/texture/cd_phm_01_sword_handle_0278_n.dds", root)
            material_entry = _entry("character/texture/cd_phm_01_sword_handle_0278_ma.dds", root)
            sidecar_entry = _entry(
                "character/modelproperty/1_pc/1_phm/weapon/1_onehandweapon/cd_phm_01_sword_0278.pac_xml",
                root,
            )
            original_refs = (
                ArchiveModelTextureReference(
                    reference_name=base_entry.path,
                    material_name="Blade",
                    sidecar_parameter_name="_overlayColorTexture",
                    resolved_archive_path=base_entry.path,
                    resolved_entry=base_entry,
                ),
                ArchiveModelTextureReference(
                    reference_name=normal_entry.path,
                    material_name="Handle.002",
                    sidecar_parameter_name="_normalTexture",
                    resolved_archive_path=normal_entry.path,
                    resolved_entry=normal_entry,
                ),
                ArchiveModelTextureReference(
                    reference_name=material_entry.path,
                    material_name="Handle.002",
                    sidecar_parameter_name="_colorBlendingMaskTexture",
                    resolved_archive_path=material_entry.path,
                    resolved_entry=material_entry,
                ),
            )
            sidecar_text = (
                '<Root><CDMaterialWrapper _subMeshName="Handle.002"><Vector Name="_parameters">'
                '<MaterialParameterTexture StringItemID="_normalTexture" _name="_normalTexture" Index="0">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_phm_01_sword_handle_0278_n.dds"/>'
                '</MaterialParameterTexture>'
                '<MaterialParameterTexture StringItemID="_heightTexture" _name="_heightTexture" Index="1">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_phm_01_sword_handle_0278_disp.dds"/>'
                '</MaterialParameterTexture>'
                '<MaterialParameterTexture StringItemID="_colorBlendingMaskTexture" ItemID="3936485985222654" _name="_colorBlendingMaskTexture" Index="2">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_phm_01_sword_handle_0278_ma.dds"/>'
                '</MaterialParameterTexture>'
                '<MaterialParameterTexture StringItemID="_grimeDiffuseTextureG" _name="_grimeDiffuseTextureG" Index="3">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_texturelayer_003_0101.dds"/>'
                '</MaterialParameterTexture>'
                "</Vector></CDMaterialWrapper></Root>"
            )
            obj_mesh = ParsedMesh(
                submeshes=[
                    SubMesh(
                        name="Handle.002",
                        material="Handle.002",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ]
            )
            rebuilt_mesh = ParsedMesh(
                submeshes=[
                    SubMesh(
                        name="Handle.002",
                        material="Handle.002",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ]
            )
            mappings = (
                StaticSubmeshMapping(
                    target_submesh_index=0,
                    target_submesh_name="Handle.002",
                    source_submesh_indices=[0],
                    target_material_slot_index=0,
                ),
            )

            def fake_texconv(command: list[str], **_kwargs: object) -> tuple[int, str, str]:
                out_dir = Path(command[command.index("-o") + 1])
                width = int(command[command.index("-w") + 1])
                height = int(command[command.index("-h") + 1])
                mips = int(command[command.index("-m") + 1])
                produced = out_dir / f"{Path(command[-1]).stem}.dds"
                produced.write_bytes(_fake_dds_bytes(width, height, mips=mips))
                return 0, "", ""

            with patch("cdmw.core.common.run_process_with_cancellation", side_effect=fake_texconv):
                payloads, report = build_texture_replacement_payloads(
                    obj_mesh=obj_mesh,
                    rebuilt_mesh=rebuilt_mesh,
                    texture_files=(handle_base, handle_normal, handle_metallic, handle_roughness),
                    original_texture_refs=original_refs,
                    original_sidecars=((sidecar_entry, sidecar_text),),
                    submesh_mappings=mappings,
                    texconv_path=texconv,
                    read_original_texture_bytes=lambda entry: (
                        template_base_dds.read_bytes()
                        if entry is base_entry
                        else template_material_dds.read_bytes()
                        if entry is material_entry
                        else template_normal_dds.read_bytes()
                    ),
                    original_texture_source_path=lambda entry: (
                        template_base_dds
                        if entry is base_entry
                        else template_material_dds
                        if entry is material_entry
                        else template_normal_dds
                    ),
                    pac_driven_sidecar=True,
                )

            payloads_by_path = {payload.target_path: payload for payload in payloads}
            self.assertIn(sidecar_entry.path, payloads_by_path)
            texture_payloads = [payload for payload in payloads if payload.kind == "texture_generated"]
            self.assertEqual(2, len(texture_payloads))
            self.assertTrue(any(payload.target_path.endswith("handle_002_basecolor.dds") for payload in texture_payloads))
            self.assertTrue(any(payload.target_path.endswith("handle_002_normal.dds") for payload in texture_payloads))
            self.assertFalse(any(payload.target_path.endswith("handle_002_metallic.dds") for payload in texture_payloads))
            self.assertFalse(any(payload.target_path.endswith("handle_002_roughness.dds") for payload in texture_payloads))
            patched_sidecar = payloads_by_path[sidecar_entry.path].payload_data.decode("utf-8")
            self.assertIn("_overlayColorTexture", patched_sidecar)
            self.assertIn("_normalTexture", patched_sidecar)
            self.assertNotIn("_metallicTexture", patched_sidecar)
            self.assertNotIn("_roughnessTexture", patched_sidecar)
            self.assertNotIn("_ambientOcclusionTexture", patched_sidecar)
            self.assertIn('ItemID="3936485985222654" _name="_overlayColorTexture"', patched_sidecar)
            self.assertNotIn("_colorBlendingMaskTexture", patched_sidecar)
            self.assertIn("_grimeDiffuseTextureG", patched_sidecar)
            self.assertIn("cd_texturelayer_003_0101.dds", patched_sidecar)
            self.assertNotIn("character/texture/cd_phm_01_sword_handle_0278_ma.dds", patched_sidecar)
            self.assertIn("character/texture/cd_phm_01_sword_0278_handle_002_basecolor.dds", patched_sidecar)
            self.assertTrue(
                any(mapping.slot_kind == "base" and mapping.output_texture_path.endswith("handle_002_basecolor.dds") for mapping in report.slot_mappings)
            )

    def test_pac_driven_sidecar_honors_manual_texture_slot_overrides_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texconv = root / "texconv.exe"
            texconv.write_bytes(b"fake")
            base_source = root / "CD_PHW_00_Nude_0001.dds"
            normal_source = root / "CD_PHW_00_Nude_0001_n.dds"
            base_source.write_bytes(_fake_dds_bytes(1024, 1024, mips=11))
            normal_source.write_bytes(_fake_dds_bytes(1024, 1024, mips=11))
            template_base = root / "template_base.dds"
            template_normal = root / "template_normal.dds"
            template_base.write_bytes(_fake_dds_bytes(2048, 2048, mips=12))
            template_normal.write_bytes(_fake_dds_bytes(1024, 2048, mips=12))
            base_entry = _entry("character/texture/cd_phw_00_nude_00_0001.dds", root)
            normal_entry = _entry("character/texture/cd_phw_00_nude_00_0001_n.dds", root)
            sidecar_entry = _entry("character/modelproperty/1_pc/2_phw/nude/cd_phw_00_nude_00_0001_damian.pac_xml", root)
            original_refs = (
                ArchiveModelTextureReference(
                    reference_name=base_entry.path,
                    material_name="CD_PHW_00_Nude_00_0001",
                    sidecar_parameter_name="_overlayColorTexture",
                    resolved_archive_path=base_entry.path,
                    resolved_entry=base_entry,
                ),
                ArchiveModelTextureReference(
                    reference_name=normal_entry.path,
                    material_name="CD_PHW_00_Nude_00_0001",
                    sidecar_parameter_name="_normalTexture",
                    resolved_archive_path=normal_entry.path,
                    resolved_entry=normal_entry,
                ),
            )
            sidecar_text = (
                '<Root><CDMaterialWrapper _subMeshName="CD_PHW_00_Nude_00_0001"><Vector Name="_parameters">'
                '<MaterialParameterTexture StringItemID="_overlayColorTexture" _name="_overlayColorTexture" Index="0">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_phw_00_nude_00_0001.dds"/>'
                '</MaterialParameterTexture>'
                '<MaterialParameterTexture StringItemID="_normalTexture" _name="_normalTexture" Index="1">'
                '<ResourceReferencePath_ITexture Name="_value" _path="character/texture/cd_phw_00_nude_00_0001_n.dds"/>'
                '</MaterialParameterTexture>'
                "</Vector></CDMaterialWrapper></Root>"
            )
            replacement_mesh = ParsedMesh(
                submeshes=[
                    SubMesh(
                        name="CD_PHW_00_Nude_0001",
                        material="CD_PHW_00_Nude_0001",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ]
            )
            rebuilt_mesh = ParsedMesh(
                submeshes=[
                    SubMesh(
                        name="CD_PHW_00_Nude_00_0001",
                        material="CD_PHW_00_Nude_00_0001",
                        vertices=[(0.0, 0.0, 0.0)],
                        faces=[(0, 0, 0)],
                    )
                ]
            )
            mappings = (
                StaticSubmeshMapping(
                    target_submesh_index=0,
                    target_submesh_name="CD_PHW_00_Nude_00_0001",
                    source_submesh_indices=[0],
                    target_material_slot_index=0,
                ),
            )

            with patch("cdmw.core.common.run_process_with_cancellation") as fake_texconv:
                fake_texconv.return_value = (0, "", "")
                payloads, report = build_texture_replacement_payloads(
                    obj_mesh=replacement_mesh,
                    rebuilt_mesh=rebuilt_mesh,
                    texture_files=(base_source, normal_source),
                    original_texture_refs=original_refs,
                    original_sidecars=((sidecar_entry, sidecar_text),),
                    submesh_mappings=mappings,
                    texconv_path=None,
                    read_original_texture_bytes=lambda entry: template_base.read_bytes() if entry is base_entry else template_normal.read_bytes(),
                    original_texture_source_path=lambda entry: template_base if entry is base_entry else template_normal,
                    texture_slot_overrides=(
                        StaticTextureSlotOverride(
                            target_texture_path=base_entry.path,
                            source_path=str(base_source),
                            slot_kind="base",
                            target_material_name="CD_PHW_00_Nude_00_0001",
                        ),
                        StaticTextureSlotOverride(
                            target_texture_path=normal_entry.path,
                            source_path=str(normal_source),
                            slot_kind="normal",
                            target_material_name="CD_PHW_00_Nude_00_0001",
                        ),
                    ),
                    pac_driven_sidecar=True,
                )

            payloads_by_path = {payload.target_path: payload for payload in payloads}
            self.assertIn(base_entry.path, payloads_by_path)
            self.assertIn(normal_entry.path, payloads_by_path)
            self.assertIn("Applied 2 manual texture slot override(s).", report.warnings)
            self.assertTrue(
                any(
                    mapping.target_texture_path == base_entry.path
                    and mapping.output_texture_path == base_entry.path
                    and mapping.slot_kind == "base"
                    for mapping in report.slot_mappings
                )
            )
            self.assertTrue(
                any(
                    mapping.target_texture_path == normal_entry.path
                    and mapping.output_texture_path == normal_entry.path
                    and mapping.slot_kind == "normal"
                    for mapping in report.slot_mappings
                )
            )
            self.assertNotIn(sidecar_entry.path, payloads_by_path)
            self.assertIn(
                "PAC-driven texture payloads were built, but no .pac_xml sidecar changes were applied. "
                "This is expected only when texture paths are overwritten in-place.",
                report.warnings,
            )


if __name__ == "__main__":
    unittest.main()
