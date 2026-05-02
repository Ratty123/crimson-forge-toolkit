import tempfile
import unittest
from pathlib import Path

from cdmw.core.archive_relationships import (
    ARCHIVE_REL_INCLUDE_MANUAL,
    ARCHIVE_REL_INCLUDE_REQUIRED,
    SWAP_SCOPE_BODY_HEAD,
    build_archive_relationship_plan,
    build_character_swap_plan,
    resolve_material_texture_graph,
)
from cdmw.core.archive import (
    build_archive_entry_basename_index,
    build_archive_entry_path_index,
    build_archive_preview_result,
)
from cdmw.models import ArchiveEntry


class ArchiveRelationshipTests(unittest.TestCase):
    def _entries(self, payloads):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        paz_path = root / "0.paz"
        pamt_path = root / "0.pamt"
        offset = 0
        entries = []
        with paz_path.open("wb") as handle:
            for index, (path, payload) in enumerate(payloads):
                data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
                handle.write(data)
                entries.append(
                    ArchiveEntry(
                        path=path,
                        pamt_path=pamt_path,
                        paz_file=paz_path,
                        offset=offset,
                        comp_size=len(data),
                        orig_size=len(data),
                        flags=0,
                        paz_index=0,
                    )
                )
                offset += len(data)
        return tuple(entries)

    def test_model_sidecar_resolves_exact_dds_paths(self):
        entries = self._entries(
            (
                ("character/model/body.pac", b"PAR "),
                (
                    "character/modelproperty/body.pac_xml",
                    '<Param name="_subMeshName" value="Body"/><ResourceReferencePath_ITexture value="character/texture/body.dds"/>',
                ),
                ("character/texture/body.dds", b"DDS "),
            )
        )

        plan = resolve_material_texture_graph(entries[0], entries)

        self.assertTrue(any(edge.relation_kind == "material_sidecar" for edge in plan.edges))
        texture_edges = [edge for edge in plan.edges if edge.relation_kind == "texture"]
        self.assertEqual([edge.related_path for edge in texture_edges], ["character/texture/body.dds"])
        self.assertEqual(texture_edges[0].confidence, "exact_path")

    def test_app_xml_graph_reaches_prefab_model_sidecar_and_textures(self):
        entries = self._entries(
            (
                ("character/appearance/a.app_xml", '<Appearance><Nude Name="body_a" /><Customization MeshParamFile="meshparam_a" /></Appearance>'),
                ("character/prefab/body_a.prefabdata_xml", '<Prefab FileName="body_a.pac" />'),
                ("character/model/body_a.pac", b"PAR "),
                ("character/modelproperty/body_a.pac_xml", '<ResourceReferencePath_ITexture value="character/texture/body_a.dds"/>'),
                ("character/texture/body_a.dds", b"DDS "),
                ("character/customization/meshparam_a.xml", "<MeshParam />"),
            )
        )

        plan = build_archive_relationship_plan(entries[0], entries)
        paths = {edge.related_path for edge in plan.edges}

        self.assertIn("character/prefab/body_a.prefabdata_xml", paths)
        self.assertIn("character/model/body_a.pac", paths)
        self.assertIn("character/modelproperty/body_a.pac_xml", paths)
        self.assertIn("character/texture/body_a.dds", paths)
        self.assertIn("character/customization/meshparam_a.xml", paths)

    def test_app_xml_preview_referenced_files_uses_relationship_graph(self):
        entries = self._entries(
            (
                ("character/appearance/a.app_xml", '<Appearance><Nude Name="body_a" /><Customization MeshParamFile="meshparam_a" /></Appearance>'),
                ("character/prefab/body_a.prefabdata_xml", '<Prefab FileName="body_a.pac" />'),
                ("character/model/body_a.pac", b"PAR "),
                ("character/modelproperty/body_a.pac_xml", '<ResourceReferencePath_ITexture value="character/texture/body_a.dds"/>'),
                ("character/texture/body_a.dds", b"DDS "),
                ("character/customization/meshparam_a.xml", "<MeshParam />"),
            )
        )

        result = build_archive_preview_result(
            None,
            entries[0],
            texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
            texture_entries_by_basename=build_archive_entry_basename_index(entries),
        )
        paths = {reference.resolved_archive_path for reference in result.model_texture_references}

        self.assertIn("character/prefab/body_a.prefabdata_xml", paths)
        self.assertIn("character/model/body_a.pac", paths)
        self.assertIn("character/modelproperty/body_a.pac_xml", paths)
        self.assertIn("character/texture/body_a.dds", paths)
        self.assertIn("character/customization/meshparam_a.xml", paths)

    def test_prefabdata_preview_referenced_files_resolves_model_skeleton_and_physics(self):
        entries = self._entries(
            (
                (
                    "character/prefab/body_a.prefabdata_xml",
                    '<Prefab FileName="body_a.pac" SkeletonName="identityskeleton.pab" RagdollName="body_a.hkx" />',
                ),
                ("character/model/body_a.pac", b"PAR "),
                ("character/modelproperty/body_a.pac_xml", '<ResourceReferencePath_ITexture value="character/texture/body_a.dds"/>'),
                ("character/texture/body_a.dds", b"DDS "),
                ("character/identityskeleton.pab", b"PAB"),
                ("character/bin/body_a.hkx", b"HKX"),
            )
        )

        result = build_archive_preview_result(
            None,
            entries[0],
            texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
            texture_entries_by_basename=build_archive_entry_basename_index(entries),
        )
        paths = {reference.resolved_archive_path for reference in result.model_texture_references}

        self.assertIn("character/model/body_a.pac", paths)
        self.assertIn("character/modelproperty/body_a.pac_xml", paths)
        self.assertIn("character/texture/body_a.dds", paths)
        self.assertIn("character/identityskeleton.pab", paths)
        self.assertIn("character/bin/body_a.hkx", paths)

    def test_material_sidecar_preview_referenced_files_dedupes_graph_texture(self):
        entries = self._entries(
            (
                ("character/model/body_a.pac", b"PAR "),
                ("character/modelproperty/body_a.pac_xml", '<ResourceReferencePath_ITexture value="character/texture/body_a.dds"/>'),
                ("character/texture/body_a.dds", b"DDS "),
            )
        )

        result = build_archive_preview_result(
            None,
            entries[1],
            texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
            texture_entries_by_basename=build_archive_entry_basename_index(entries),
        )
        paths = [reference.resolved_archive_path for reference in result.model_texture_references]

        self.assertEqual(paths.count("character/texture/body_a.dds"), 1)

    def test_direct_pam_and_pamlod_sidecar_previews_resolve_dds(self):
        for sidecar_path in (
            "character/modelproperty/body_a.pam_xml",
            "character/modelproperty/body_a.pamlod_xml",
        ):
            with self.subTest(sidecar_path=sidecar_path):
                entries = self._entries(
                    (
                        (
                            sidecar_path,
                            '<MaterialParameterTexture _name="_baseColorTexture">'
                            '<ResourceReferencePath_ITexture value="character/texture/body_a_d.dds"/>'
                            "</MaterialParameterTexture>",
                        ),
                        ("character/texture/body_a_d.dds", b"DDS "),
                    )
                )

                result = build_archive_preview_result(
                    None,
                    entries[0],
                    texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
                    texture_entries_by_basename=build_archive_entry_basename_index(entries),
                )
                paths = {reference.resolved_archive_path for reference in result.model_texture_references}

                self.assertIn("character/texture/body_a_d.dds", paths)

    def test_sidecar_graph_preserves_distinct_texture_parameter_roles(self):
        entries = self._entries(
            (
                (
                    "character/modelproperty/body_a.pac_xml",
                    '<SkinnedMeshMaterialWrapper _subMeshName="Body">'
                    '<MaterialParameterTexture _name="_baseColorTexture"><ResourceReferencePath_ITexture value="character/texture/body_a_d.dds"/></MaterialParameterTexture>'
                    '<MaterialParameterTexture _name="_normalTexture"><ResourceReferencePath_ITexture value="character/texture/body_a_n.dds"/></MaterialParameterTexture>'
                    '<MaterialParameterTexture _name="_materialTexture"><ResourceReferencePath_ITexture value="character/texture/body_a_ma.dds"/></MaterialParameterTexture>'
                    '<MaterialParameterTexture _name="_heightTexture"><ResourceReferencePath_ITexture value="character/texture/body_a_disp.dds"/></MaterialParameterTexture>'
                    '<MaterialParameterTexture _name="_maskTexture"><ResourceReferencePath_ITexture value="character/texture/body_a_mask.dds"/></MaterialParameterTexture>'
                    "</SkinnedMeshMaterialWrapper>",
                ),
                ("character/texture/body_a_d.dds", b"DDS "),
                ("character/texture/body_a_n.dds", b"DDS "),
                ("character/texture/body_a_ma.dds", b"DDS "),
                ("character/texture/body_a_disp.dds", b"DDS "),
                ("character/texture/body_a_mask.dds", b"DDS "),
            )
        )

        result = build_archive_preview_result(
            None,
            entries[0],
            texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
            texture_entries_by_basename=build_archive_entry_basename_index(entries),
        )
        by_path = {reference.resolved_archive_path: reference for reference in result.model_texture_references}

        self.assertEqual(
            {
                "character/texture/body_a_d.dds",
                "character/texture/body_a_n.dds",
                "character/texture/body_a_ma.dds",
                "character/texture/body_a_disp.dds",
                "character/texture/body_a_mask.dds",
            },
            set(by_path),
        )
        self.assertEqual(by_path["character/texture/body_a_d.dds"].semantic_label, "Base Color Texture")
        self.assertEqual(by_path["character/texture/body_a_n.dds"].semantic_label, "Normal Texture")

    def test_app_xml_duplicate_basename_prefers_path_local_graph(self):
        entries = self._entries(
            (
                ("character/a/appearance/body.app_xml", '<Appearance><Nude Name="body" /></Appearance>'),
                ("character/a/prefab/body.prefabdata_xml", '<Prefab FileName="body.pac" />'),
                ("character/a/model/body.pac", b"PAR "),
                ("character/a/modelproperty/body.pac_xml", '<ResourceReferencePath_ITexture value="character/a/texture/body.dds"/>'),
                ("character/a/texture/body.dds", b"DDS A"),
                ("character/b/prefab/body.prefabdata_xml", '<Prefab FileName="body.pac" />'),
                ("character/b/model/body.pac", b"PAR "),
                ("character/b/modelproperty/body.pac_xml", '<ResourceReferencePath_ITexture value="character/b/texture/body.dds"/>'),
                ("character/b/texture/body.dds", b"DDS B"),
            )
        )

        result = build_archive_preview_result(
            None,
            entries[0],
            texture_entries_by_normalized_path=build_archive_entry_path_index(entries),
            texture_entries_by_basename=build_archive_entry_basename_index(entries),
        )
        paths = {reference.resolved_archive_path for reference in result.model_texture_references}

        self.assertIn("character/a/prefab/body.prefabdata_xml", paths)
        self.assertIn("character/a/model/body.pac", paths)
        self.assertIn("character/a/texture/body.dds", paths)
        self.assertNotIn("character/b/prefab/body.prefabdata_xml", paths)
        self.assertNotIn("character/b/model/body.pac", paths)
        self.assertNotIn("character/b/texture/body.dds", paths)

    def test_character_swap_patch_changes_body_and_head_only(self):
        entries = self._entries(
            (
                (
                    "character/appearance/target.app_xml",
                    '<Appearance><Nude Name="target_body" CharacterScale="1.0" /><Head Name="target_head" /><Hair Name="target_hair" /></Appearance>',
                ),
                (
                    "character/appearance/source.app_xml",
                    '<Appearance><Nude Name="source_body" CharacterScale="1.2" /><Head Name="source_head" /><Hair Name="source_hair" /></Appearance>',
                ),
                ("character/prefab/source_body.prefabdata_xml", "<Prefab />"),
                ("character/prefab/source_head.prefabdata_xml", "<Prefab />"),
            )
        )

        plan = build_character_swap_plan(entries[0], entries[1], entries, swap_scope=SWAP_SCOPE_BODY_HEAD)
        patched = plan.patched_target_app_xml.decode("utf-8")

        self.assertEqual(plan.patched_target_app_path, "character/appearance/target.app_xml")
        self.assertIn("source_body", patched)
        self.assertIn("source_head", patched)
        self.assertIn("target_hair", patched)
        self.assertNotIn("source_hair", patched)
        self.assertTrue(any(edge.relation_kind == "appearance_patch" and edge.include_policy == ARCHIVE_REL_INCLUDE_REQUIRED for edge in plan.edges))

    def test_duplicate_dds_basenames_are_not_collapsed_for_exact_path(self):
        entries = self._entries(
            (
                ("object/model/rock.pam", b"PAR "),
                ("object/model/rock.pami", '<ResourceReferencePath_ITexture value="object/texture/b/shared.dds"/>'),
                ("object/texture/a/shared.dds", b"DDS A"),
                ("object/texture/b/shared.dds", b"DDS B"),
            )
        )

        plan = resolve_material_texture_graph(entries[0], entries)
        texture_paths = [edge.related_path for edge in plan.edges if edge.relation_kind == "texture"]

        self.assertEqual(texture_paths, ["object/texture/b/shared.dds"])

    def test_skeleton_physics_and_missing_descriptors_are_manual_or_unresolved(self):
        entries = self._entries(
            (
                ("character/prefab/body.prefabdata_xml", '<Prefab SkeletonName="identityskeleton.pab" RagdollName="body.hkx" MissingName="missing.pabc" />'),
                ("character/identityskeleton.pab", b"PAB"),
                ("character/bin/body.hkx", b"HKX"),
            )
        )

        plan = build_archive_relationship_plan(entries[0], entries)
        skeleton = next(edge for edge in plan.edges if edge.relation_kind == "skeleton")
        physics = next(edge for edge in plan.edges if edge.relation_kind == "physics")
        unresolved = next(edge for edge in plan.edges if edge.unresolved)

        self.assertEqual(skeleton.include_policy, ARCHIVE_REL_INCLUDE_MANUAL)
        self.assertTrue(skeleton.risk)
        self.assertEqual(physics.include_policy, ARCHIVE_REL_INCLUDE_MANUAL)
        self.assertTrue(physics.risk)
        self.assertEqual(unresolved.related_path, "missing.pabc")

    def test_sidecar_topology_difference_is_reported_for_character_swap(self):
        entries = self._entries(
            (
                ("character/model/target.pac", b"PAR "),
                ("character/model/source.pac", b"PAR "),
                ("character/modelproperty/target.pac_xml", '<Param name="_subMeshName" value="TargetBody"/>'),
                ("character/modelproperty/source.pac_xml", '<Param name="_subMeshName" value="SourceBody"/>'),
                ("character/appearance/target.app_xml", '<Appearance><Nude Name="target" /></Appearance>'),
                ("character/appearance/source.app_xml", '<Appearance><Nude Name="source" /></Appearance>'),
            )
        )

        plan = build_character_swap_plan(entries[0], entries[1], entries)

        self.assertTrue(any("submesh wrappers differ" in warning for warning in plan.warnings))
        self.assertTrue(any(edge.role == "topology_reference" and edge.risk for edge in plan.edges))


if __name__ == "__main__":
    unittest.main()
