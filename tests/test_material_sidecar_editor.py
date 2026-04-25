from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cdmw.core.material_sidecar_editor import (
    apply_material_sidecar_edits,
    detect_material_sidecar_related_files,
    detect_material_sidecar_preview_model_candidates,
    discover_material_sidecar_preview_overrides,
    discover_material_sidecar_values,
    export_material_sidecar_mod_package,
)
from cdmw.core.upscale_profiles import parse_texture_sidecar_bindings
from cdmw.models import ArchiveEntry, ArchiveModelTextureReference, ModPackageInfo


def entry(path: str, root: Path) -> ArchiveEntry:
    return ArchiveEntry(
        path=path,
        pamt_path=root / "package" / "pad00000_meta.pamt",
        paz_file=root / "package" / "pad00000.paz",
        offset=0,
        comp_size=1,
        orig_size=1,
        flags=0,
        paz_index=0,
    )


class MaterialSidecarEditorTests(unittest.TestCase):
    def test_discovers_material_values_from_wrapped_multi_root_fragment(self) -> None:
        text = """
        <SkinnedMeshMaterialWrapper _subMeshName="cloak">
          <RepresentColor x="1" y="0.5" z="0.25" />
          <Material>
            <MaterialParameterColor _name="_tintColor" x="0.8" y="0.7" z="0.6" />
            <MaterialParameterFloat _name="_brightness" Value="1.2" />
            <MaterialParameterTexture _name="_overlayColorTexture">
              <ResourceReferencePath_ITexture _path="character/texture/cd_phm_00_cloak_00_0340.dds" />
            </MaterialParameterTexture>
          </Material>
        </SkinnedMeshMaterialWrapper>
        <SkinnedMeshMaterialWrapper _subMeshName="trim">
          <MaterialParameterFloat _name="_uvScale" Value="2" />
        </SkinnedMeshMaterialWrapper>
        """

        rows = discover_material_sidecar_values(text)
        names = {(row.kind, row.group_label, row.parameter_name, row.value) for row in rows}

        self.assertIn(("color", "cloak", "RepresentColor", "1, 0.5, 0.25"), names)
        self.assertIn(("color", "cloak", "_tintColor", "0.8, 0.7, 0.6"), names)
        self.assertIn(("float", "cloak", "_brightness", "1.2"), names)
        self.assertIn(("float", "trim", "_uvScale", "2"), names)
        self.assertIn(
            (
                "texture",
                "cloak",
                "_overlayColorTexture",
                "character/texture/cd_phm_00_cloak_00_0340.dds",
            ),
            names,
        )

    def test_applies_color_float_and_texture_edits_without_touching_unrelated_values(self) -> None:
        text = """
        <SkinnedMeshMaterialWrapper _subMeshName="cloak">
          <MaterialParameterColor _name="_tintColor" x="0.8" y="0.7" z="0.6" />
          <MaterialParameterFloat _name="_brightness" Value="1.2" />
          <MaterialParameterFloat _name="_unrelated" Value="9" />
          <MaterialParameterTexture _name="_overlayColorTexture">
            <ResourceReferencePath_ITexture _path="old.dds" />
          </MaterialParameterTexture>
        </SkinnedMeshMaterialWrapper>
        """
        rows = {row.parameter_name: row for row in discover_material_sidecar_values(text)}
        result = apply_material_sidecar_edits(
            text,
            {
                rows["_tintColor"].row_id: "#000000",
                rows["_brightness"].row_id: "0.75",
                rows["_overlayColorTexture"].row_id: "new/path.dds",
            },
        )

        self.assertIn('_name="_tintColor" x="0" y="0" z="0"', result.text)
        self.assertIn('_name="_brightness" Value="0.75"', result.text)
        self.assertIn('_name="_unrelated" Value="9"', result.text)
        self.assertIn('_path="new/path.dds"', result.text)
        self.assertEqual(3, len(result.changed_rows))

    def test_discovers_and_applies_hex_rgba_material_color_values(self) -> None:
        text = """
        <SkinnedMeshMaterialWrapper _subMeshName="cloak">
          <MaterialParameterColor _name="_tintColorR" _value="#4d1708ff" />
          <MaterialParameterColor _name="_dyeingColorMaskG" _value="#c3b3af4c" />
        </SkinnedMeshMaterialWrapper>
        """
        rows = {row.parameter_name: row for row in discover_material_sidecar_values(text)}

        self.assertEqual("#4d1708ff", rows["_tintColorR"].value)
        result = apply_material_sidecar_edits(text, {rows["_tintColorR"].row_id: "#050505"})

        self.assertIn('_name="_tintColorR" _value="#050505ff"', result.text)
        self.assertIn('_name="_dyeingColorMaskG" _value="#c3b3af4c"', result.text)

    def test_detects_same_stem_op_and_explicit_related_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = entry("character/modelproperty/cd_phm_00_cloak_00_0340.pac_xml", root)
            mesh = entry("character/model/cd_phm_00_cloak_00_0340.pac", root)
            base = entry("character/texture/cd_phm_00_cloak_00_0340.dds", root)
            op = entry("character/texture/cd_phm_00_cloak_00_0340_op.dds", root)
            explicit = entry("character/texture/explicit_override.dds", root)
            basename_index = {
                mesh.basename.lower(): [mesh],
                base.basename.lower(): [base],
                op.basename.lower(): [op],
                explicit.basename.lower(): [explicit],
            }
            references = (
                ArchiveModelTextureReference(
                    reference_name="explicit_override.dds",
                    resolved_archive_path=explicit.path,
                    resolved_entry=explicit,
                ),
            )

            related = detect_material_sidecar_related_files(
                sidecar,
                references=references,
                archive_entries_by_basename=basename_index,
            )
            by_path = {item.entry.path: item for item in related}

            self.assertEqual("explicit", by_path[explicit.path].confidence)
            self.assertIn(mesh.path, by_path)
            self.assertIn(base.path, by_path)
            self.assertIn(op.path, by_path)

    def test_detects_same_stem_pac_preview_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = entry("character/modelproperty/cd_phm_00_cloak_00_0340.pac_xml", root)
            mesh = entry("character/modelproperty/cd_phm_00_cloak_00_0340.pac", root)
            candidates = detect_material_sidecar_preview_model_candidates(
                sidecar,
                archive_entries_by_basename={mesh.basename.lower(): [mesh]},
            )

            self.assertEqual(mesh.path, candidates[0].entry.path)
            self.assertEqual("same-stem", candidates[0].confidence)

    def test_detects_op_sidecar_preview_model_family(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = entry("character/modelproperty/cd_phm_00_cloak_00_0340_op.pac_xml", root)
            op_mesh = entry("character/modelproperty/cd_phm_00_cloak_00_0340_op.pac", root)
            base_mesh = entry("character/modelproperty/cd_phm_00_cloak_00_0340.pac", root)
            candidates = detect_material_sidecar_preview_model_candidates(
                sidecar,
                archive_entries_by_basename={
                    op_mesh.basename.lower(): [op_mesh],
                    base_mesh.basename.lower(): [base_mesh],
                },
            )

            self.assertEqual(op_mesh.path, candidates[0].entry.path)
            self.assertIn(base_mesh.path, {candidate.entry.path for candidate in candidates})

    def test_detects_pami_explicit_preview_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = entry("character/modelproperty/example.pami", root)
            mesh = entry("character/model/static/example.pam", root)
            text = '<StaticMesh Path="character/model/static/example.pam" />'
            candidates = detect_material_sidecar_preview_model_candidates(
                sidecar,
                sidecar_text=text,
                archive_entries_by_normalized_path={mesh.path.lower(): [mesh]},
            )

            self.assertEqual(mesh.path, candidates[0].entry.path)
            self.assertEqual("explicit", candidates[0].confidence)

    def test_discovers_preview_overrides_for_tint_brightness_and_uv(self) -> None:
        text = """
        <Material PrimitiveName="cloak">
          <MaterialParameterColor _name="_tintColor" _value="#204060ff" />
          <MaterialParameterFloat _name="_brightness" _value="1.4" />
          <MaterialParameterFloat _name="_uvScale" _value="2.5" />
          <MaterialParameterTexture _name="_baseColorTexture">
            <ResourceReferencePath_ITexture _path="character/texture/example.dds" />
          </MaterialParameterTexture>
        </Material>
        """
        overrides = discover_material_sidecar_preview_overrides(text)
        bindings = parse_texture_sidecar_bindings(text, sidecar_path="character/modelproperty/example.pami")

        self.assertEqual("cloak", overrides[0].group_label)
        self.assertAlmostEqual(0x20 / 255.0, overrides[0].tint_color[0])
        self.assertEqual(1.4, overrides[0].brightness)
        self.assertEqual(2.5, overrides[0].uv_scale)
        self.assertAlmostEqual(0x20 / 255.0, bindings[0].tint_color[0])
        self.assertEqual(1.4, bindings[0].brightness)
        self.assertEqual(2.5, bindings[0].uv_scale)

    def test_discovers_preview_overrides_for_cloak_dye_channels(self) -> None:
        text = """
        <SkinnedMeshMaterialWrapper _subMeshName="cloak">
          <MaterialParameterColor _name="_tintColorR" _value="#050505ff" />
          <MaterialParameterColor _name="_dyeingColorMaskG" _value="#1111114c" />
          <MaterialParameterColor _name="_dyeingDetailLayerColorMaskR" _value="#0a0a0aff" />
          <MaterialParameterTexture _name="_baseColorTexture">
            <ResourceReferencePath_ITexture _path="character/texture/example.dds" />
          </MaterialParameterTexture>
        </SkinnedMeshMaterialWrapper>
        """
        overrides = discover_material_sidecar_preview_overrides(text)
        bindings = parse_texture_sidecar_bindings(text, sidecar_path="character/modelproperty/example.pac_xml")

        self.assertEqual("cloak", overrides[0].group_label)
        self.assertLess(overrides[0].tint_color[0], 0.06)
        self.assertIn("dye", overrides[0].reason.lower())
        self.assertLess(bindings[0].tint_color[0], 0.06)

    def test_exports_edited_sidecar_and_related_files_with_manifest_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sidecar = entry("character/modelproperty/cd_phm_00_cloak_00_0340.pac_xml", root)
            op = entry("character/texture/cd_phm_00_cloak_00_0340_op.dds", root)
            payloads = {
                op.path: b"DDS related",
            }

            result = export_material_sidecar_mod_package(
                edited_entry=sidecar,
                edited_text="<SkinnedMeshMaterialWrapper />",
                related_entries=(op,),
                parent_root=root,
                package_info=ModPackageInfo(title="Material Edit"),
                read_entry_bytes=lambda archive_entry: payloads[archive_entry.path],
            )

            self.assertTrue((result.package_root / "character" / "modelproperty" / "cd_phm_00_cloak_00_0340.pac_xml").exists())
            self.assertTrue((result.package_root / "character" / "texture" / "cd_phm_00_cloak_00_0340_op.dds").exists())
            manifest = json.loads((result.package_root / "manifest.json").read_text(encoding="utf-8"))
            files = {item["path"]: item for item in manifest["files"]}
            self.assertIn("character/modelproperty/cd_phm_00_cloak_00_0340.pac_xml", files)
            self.assertIn("character/texture/cd_phm_00_cloak_00_0340_op.dds", files)
            self.assertIn("Edited material sidecar", files["character/modelproperty/cd_phm_00_cloak_00_0340.pac_xml"]["note"])
            self.assertTrue((result.package_root / "material_sidecar_edits.json").exists())


if __name__ == "__main__":
    unittest.main()
