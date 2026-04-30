from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cdmw.core.mod_package import (
    MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY,
    MeshLooseModAsset,
    MeshLooseModFile,
    ModPackageExportOptions,
    finalize_mod_package_export,
    write_mesh_loose_mod_package_metadata,
    write_mod_package_manifest,
)
from cdmw.models import ModPackageInfo


class ModPackageExportTests(unittest.TestCase):
    def test_universal_game_relative_metadata_is_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "ExampleMod"
            payload = root / "object" / "texture" / "sample.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            finalize_mod_package_export(
                root,
                ModPackageInfo(title="Example", version="1.2", author="Author", description="Desc", nexus_url="https://example.com"),
                kind="dds_loose_mod",
                payload_paths=("object/texture/sample.dds",),
                options=ModPackageExportOptions(structure="game_relative", create_zip=False),
            )

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            mod_json = json.loads((root / "mod.json").read_text(encoding="utf-8"))
            modinfo = json.loads((root / "modinfo.json").read_text(encoding="utf-8"))
            info_json = json.loads((root / "info.json").read_text(encoding="utf-8"))

            for key in ("title", "version", "author", "description", "nexus_url", "game", "generator", "files_dir", "manager_targets"):
                self.assertEqual(manifest.get(key), info_json.get(key), key)
                self.assertEqual(manifest.get(key), mod_json.get(key), key)
            self.assertEqual(manifest.get("manager_targets"), ["universal"])
            self.assertEqual(modinfo.get("name"), "Example")
            self.assertEqual(modinfo.get("version"), "1.2")
            self.assertEqual(modinfo.get("author"), "Author")
            self.assertEqual(modinfo.get("description"), "Desc")
            self.assertNotIn("manager_targets", modinfo)
            self.assertEqual(manifest.get("files_dir"), ".")
            self.assertNotIn("files_root", manifest)
            self.assertNotIn("new_paths", manifest)
            self.assertTrue((root / ".no_encrypt").exists())

    def test_files_wrapper_moves_payload_and_preserves_new_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "WrappedMod"
            payload = root / "object" / "texture" / "new.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            finalize_mod_package_export(
                root,
                ModPackageInfo(title="Wrapped"),
                kind="dds_loose_mod",
                payload_paths=("object/texture/new.dds",),
                new_file_paths=("object/texture/new.dds",),
                options=ModPackageExportOptions(manager_targets=("cdumm",), structure="files_wrapper"),
            )

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(payload.exists())
            self.assertTrue((root / "files" / "object" / "texture" / "new.dds").exists())
            self.assertFalse((root / "object").exists())
            self.assertEqual(manifest.get("format"), "v1")
            self.assertEqual(manifest.get("files_dir"), "files")
            self.assertEqual(manifest.get("files_root"), "files")
            self.assertEqual(manifest.get("new_paths"), ["object/texture/new.dds"])
            self.assertEqual(manifest.get("manager_targets"), ["cdumm"])

    def test_cdumm_modinfo_uses_documented_fields_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "CdummMod"
            payload = root / "object" / "texture" / "sample.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            write_mod_package_manifest(
                root,
                ModPackageInfo(title="CDUMM Example", version="2.0", author="Author", description="Desc"),
                kind="dds_loose_mod",
                export_options=ModPackageExportOptions(
                    manager_targets=("cdumm",),
                    structure="files_wrapper",
                    conflict_mode="override",
                    target_language="ko",
                ),
            )

            modinfo = json.loads((root / "modinfo.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(modinfo),
                {"name", "version", "author", "description", "conflict_mode", "target_language"},
            )
            self.assertEqual(modinfo["conflict_mode"], "override")
            self.assertEqual(modinfo["target_language"], "ko")

    def test_dmm_texture_profile_writes_texture_folder_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "DmmTextureMod"
            payload = root / "character" / "texture" / "sample.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            returned_path = write_mod_package_manifest(
                root,
                ModPackageInfo(title="DMM Texture", version="1.0", author="Author", description="Desc"),
                kind="dds_loose_mod",
                export_options=ModPackageExportOptions(
                    manager_targets=("dmm",),
                    structure="dmm_texture",
                    create_manifest_json=False,
                    create_mod_json=False,
                    create_info_json=False,
                    create_no_encrypt_file=False,
                ),
            )

            self.assertTrue((root / "character" / "texture" / "sample.dds").exists())
            self.assertTrue((root / "modinfo.json").exists())
            self.assertFalse((root / "files").exists())
            self.assertFalse((root / "manifest.json").exists())
            self.assertFalse((root / "mod.json").exists())
            self.assertFalse((root / "info.json").exists())
            self.assertFalse((root / ".no_encrypt").exists())
            self.assertEqual(returned_path.name, "modinfo.json")
            modinfo = json.loads((root / "modinfo.json").read_text(encoding="utf-8"))
            self.assertEqual(set(modinfo), {"name", "version", "author", "description"})
            readme_text = (root / "README.txt").read_text(encoding="utf-8")
            self.assertIn("mods/_textures/", readme_text)
            self.assertNotIn("Preferred manager", readme_text)
            self.assertNotIn("nexusmods.com/crimsondesert/mods/113", readme_text)

    def test_custom_compact_paths_uses_files_wrapper_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "CompactMod"
            payload = root / "character" / "sample.pac"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"PAC ")

            finalize_mod_package_export(
                root,
                ModPackageInfo(title="Compact"),
                kind="mesh_loose_mod",
                payload_paths=("character/sample.pac",),
                options=ModPackageExportOptions(structure="custom_compact_paths"),
            )

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertFalse(payload.exists())
            self.assertTrue((root / "files" / "character" / "sample.pac").exists())
            self.assertFalse((root / "character").exists())
            self.assertEqual(manifest.get("structure"), "custom_compact_paths")
            self.assertEqual(manifest.get("files_dir"), "files")
            self.assertEqual(manifest.get("files_root"), "files")

    def test_mesh_loose_mod_coerces_dmm_texture_structure_to_mesh_safe_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "MeshDmmSafe"
            payload = root / "character" / "sample.pac"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"PAC ")

            finalize_mod_package_export(
                root,
                ModPackageInfo(title="Mesh DMM Safe"),
                kind="mesh_loose_mod",
                payload_paths=("character/sample.pac",),
                options=ModPackageExportOptions(manager_targets=("dmm",), structure="dmm_texture"),
            )

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "character" / "sample.pac").exists())
            self.assertEqual(manifest.get("structure"), "game_relative")
            self.assertEqual(manifest.get("files_dir"), ".")

    def test_no_encrypt_toggle_and_ready_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "ZipMod"
            payload = root / "object" / "texture" / "sample.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            result = finalize_mod_package_export(
                root,
                ModPackageInfo(title="Zip"),
                payload_paths=("object/texture/sample.dds",),
                options=ModPackageExportOptions(create_no_encrypt_file=False, create_zip=True),
            )

            self.assertFalse((root / ".no_encrypt").exists())
            self.assertIsNotNone(result.zip_path)
            assert result.zip_path is not None
            with zipfile.ZipFile(result.zip_path) as archive:
                names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("mod.json", names)
            self.assertIn("object/texture/sample.dds", names)
            self.assertNotIn(".no_encrypt", names)

    def test_metadata_artifact_table_covers_generate_options(self) -> None:
        expected = {"manifest_json", "mod_json", "modinfo_json", "info_json", "no_encrypt", "ready_zip"}
        self.assertEqual(expected, set(MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY))
        for key in expected:
            self.assertTrue(MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY[key].label)
            self.assertTrue(MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY[key].description)

    def test_mesh_manifest_records_game_index_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "MeshMod"

            write_mesh_loose_mod_package_metadata(
                root,
                ModPackageInfo(title="Mesh"),
                assets=(
                    MeshLooseModAsset(
                        entry_path="character/example.pac",
                        package_group="0009",
                        format="pac",
                        obj_path="source.obj",
                        vertices=3,
                        faces=1,
                        submeshes=1,
                    ),
                ),
                files=(
                    MeshLooseModFile(
                        path="character/example.pac",
                        package_group="0009",
                        format="pac",
                    ),
                ),
                include_paired_lod=False,
                game_build="0.papgt 0x12345678",
                game_metadata={
                    "game_build": "0.papgt 0x12345678",
                    "papgt_crc": "0x12345678",
                    "pamt_crc": "0xABCDEF01",
                },
            )

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["game_build"], "0.papgt 0x12345678")
            self.assertEqual(manifest["game_metadata"]["papgt_crc"], "0x12345678")
            self.assertEqual(manifest["game_metadata"]["pamt_crc"], "0xABCDEF01")

    def test_high_level_manifest_writer_readme_lists_generated_metadata_and_zip_contains_readme(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "ReadmeMod"
            payload = root / "object" / "texture" / "sample.dds"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"DDS ")

            write_mod_package_manifest(
                root,
                ModPackageInfo(title="Readme"),
                kind="dds_loose_mod",
                all_payload_paths=("object/texture/sample.dds",),
                export_options=ModPackageExportOptions(create_zip=True),
            )

            readme_text = (root / "README.txt").read_text(encoding="utf-8")
            self.assertIn("Crimson Desert Mod Workbench", readme_text)
            self.assertIn("Generated Loose Mod Package", readme_text)
            self.assertIn("::::::::::::-------------::---::-----:---------::::::::::", readme_text)
            self.assertIn("========     ===       ===  =====  ==  ====  ====  ======", readme_text)
            self.assertIn("+=======================================================+", readme_text)
            self.assertIn("PACKAGE\n=========================================================", readme_text)
            self.assertIn("Loose files        1", readme_text)
            self.assertNotIn("Preferred manager", readme_text)
            self.assertNotIn("preferred mod manager", readme_text)
            self.assertNotIn("nexusmods.com/crimsondesert/mods/113", readme_text)
            for expected in ("manifest.json", "mod.json", "modinfo.json", "info.json", ".no_encrypt", "ReadmeMod.zip"):
                self.assertIn(expected, readme_text)
            with zipfile.ZipFile(root.with_suffix(".zip")) as archive:
                names = set(archive.namelist())
            self.assertIn("README.txt", names)
            self.assertIn("manifest.json", names)


if __name__ == "__main__":
    unittest.main()
