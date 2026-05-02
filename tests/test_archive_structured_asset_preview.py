import tempfile
import unittest
from pathlib import Path, PurePosixPath

from cdmw.core.archive import build_simplified_text_asset_summary, build_structured_asset_preview
from cdmw.models import ArchiveEntry


def _entry(path: str, root: Path) -> ArchiveEntry:
    pamt_path = root / "0009" / "0.pamt"
    paz_path = root / "0009" / "0.paz"
    pamt_path.parent.mkdir(parents=True, exist_ok=True)
    return ArchiveEntry(
        path=path,
        pamt_path=pamt_path,
        paz_file=paz_path,
        offset=0,
        comp_size=0,
        orig_size=0,
        flags=0,
        paz_index=0,
    )


def _indexes(entries: tuple[ArchiveEntry, ...]) -> tuple[dict[str, tuple[ArchiveEntry, ...]], dict[str, tuple[ArchiveEntry, ...]]]:
    path_index: dict[str, tuple[ArchiveEntry, ...]] = {}
    basename_index: dict[str, tuple[ArchiveEntry, ...]] = {}
    for entry in entries:
        normalized_path = entry.path.replace("\\", "/").strip().lower()
        basename = PurePosixPath(normalized_path).name.lower()
        path_index.setdefault(normalized_path, ())
        path_index[normalized_path] = (*path_index[normalized_path], entry)
        basename_index.setdefault(basename, ())
        basename_index[basename] = (*basename_index[basename], entry)
    return path_index, basename_index


class ArchiveStructuredAssetPreviewTests(unittest.TestCase):
    def test_prefab_preview_resolves_model_and_motion_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _entry("character/prefab/test.prefab", root)
            model = _entry("character/model/test_model.pac", root)
            motion = _entry("character/bin__/meshphysics/test_model.hkx", root)
            path_index, basename_index = _indexes((source, model, motion))
            data = (
                b"SceneObject\x00PrefabResource\x00"
                b"character/model/test_model.pac\x00"
                b"character/bin__/meshphysics/test_model.hkx\x00"
            )

            preview = build_structured_asset_preview(
                data,
                source.path,
                extension=".prefab",
                source_entry=source,
                archive_entries_by_normalized_path=path_index,
                archive_entries_by_basename=basename_index,
            )

            self.assertIn("Prefab inspector", preview.preview_text)
            self.assertIn("Reference types: .pac: 1, .hkx: 1", preview.preview_text)
            resolved_paths = {reference.resolved_archive_path for reference in preview.related_references}
            self.assertIn(model.path, resolved_paths)
            self.assertIn(motion.path, resolved_paths)

    def test_world_navigation_preview_groups_nav_and_road_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _entry("world/nav/test.nav", root)
            data = b"NavigationGraph\x00RoadSector\x00WaypointList\x00character/prefab/road_marker.prefab\x00"

            preview = build_structured_asset_preview(data, source.path, extension=".nav", source_entry=source)

            self.assertIn("World navigation inspector", preview.preview_text)
            self.assertIn("Road / Path", preview.preview_text)
            self.assertIn("Navigation", preview.preview_text)
            self.assertIn("character/prefab/road_marker.prefab", preview.preview_text)

    def test_simplified_xml_summary_explains_material_sidecar_values(self) -> None:
        xml_text = """
        <ModelPropertyList>
          <SkinnedMeshMaterialWrapper _subMeshName="cd_test_body">
            <Material _materialName="SkinnedMeshStandard_Ver2">
              <MaterialParameterTexture _name="_normalTexture">
                <ResourceReferencePath_ITexture _path="character/texture/cd_test_body_n.dds" />
              </MaterialParameterTexture>
              <MaterialParameterColor _name="_tintColorR" _value="#aabbccff" />
            </Material>
          </SkinnedMeshMaterialWrapper>
        </ModelPropertyList>
        """

        summary = build_simplified_text_asset_summary(
            xml_text,
            extension=".pac_xml",
            virtual_path="character/modelproperty/test.pac_xml",
        )

        self.assertIn("Simplified values", summary)
        self.assertIn("Material texture bindings: 1", summary)
        self.assertIn("Submesh/material slots: cd_test_body", summary)
        self.assertIn("character/texture/cd_test_body_n.dds", summary)
        self.assertIn("guided value editor", summary)


if __name__ == "__main__":
    unittest.main()
