from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cdmw.core.mod_package import (
    MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY,
    ModPackageExportOptions,
    finalize_mod_package_export,
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
                self.assertEqual(manifest.get(key), modinfo.get(key), key)
                self.assertEqual(manifest.get(key), mod_json.get(key), key)
            self.assertEqual(manifest.get("files_dir"), ".")
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
            self.assertEqual(manifest.get("files_dir"), "files")
            self.assertEqual(manifest.get("new_paths"), ["object/texture/new.dds"])
            self.assertEqual(manifest.get("manager_targets"), ["cdumm"])

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
            for expected in ("manifest.json", "mod.json", "modinfo.json", "info.json", ".no_encrypt", "ReadmeMod.zip"):
                self.assertIn(expected, readme_text)
            with zipfile.ZipFile(root.with_suffix(".zip")) as archive:
                names = set(archive.namelist())
            self.assertIn("README.txt", names)
            self.assertIn("manifest.json", names)


if __name__ == "__main__":
    unittest.main()
