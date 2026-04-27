import unittest

from cdmw.modding.mesh_parser import ParsedMesh, SubMesh
from cdmw.modding.static_mesh_replacer import (
    StaticMeshReplacementOptions,
    StaticReplacementTransform,
    StaticSubmeshMapping,
    _build_mapped_replacement_mesh,
    build_static_replacement_preview_mesh,
)


def _mesh(path: str, submeshes: list[SubMesh]) -> ParsedMesh:
    return ParsedMesh(
        path=path,
        format="pac",
        submeshes=submeshes,
        total_vertices=sum(len(submesh.vertices) for submesh in submeshes),
        total_faces=sum(len(submesh.faces) for submesh in submeshes),
        has_uvs=any(bool(submesh.uvs) for submesh in submeshes),
    )


class StaticMeshReplacementPreviewTests(unittest.TestCase):
    def test_preview_allows_large_mapped_target_that_export_rejects(self) -> None:
        original = _mesh(
            "helmet.pac",
            [
                SubMesh(
                    name="helmet",
                    material="helmet",
                    vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                    faces=[(0, 1, 2)],
                )
            ],
        )
        replacement = _mesh(
            "large.obj",
            [
                SubMesh(
                    name="large helmet",
                    material="large helmet",
                    vertices=[(float(index), 0.0, 0.0) for index in range(70_000)],
                    faces=[(0, 1, 2)],
                )
            ],
        )
        mapping = StaticSubmeshMapping(
            target_submesh_index=0,
            target_submesh_name="helmet",
            source_submesh_indices=[0],
            target_material_slot_index=0,
        )
        options = StaticMeshReplacementOptions(
            transform=StaticReplacementTransform(
                alignment_mode="manual",
                scale_to_original_length=False,
                offset_xyz=(1.0, 2.0, 3.0),
            ),
            submesh_mappings=[mapping],
        )

        preview = build_static_replacement_preview_mesh(original, replacement, options)

        self.assertEqual(len(preview.submeshes[0].vertices), 70_000)
        self.assertEqual(preview.submeshes[0].vertices[0], (1.0, 2.0, 3.0))
        with self.assertRaisesRegex(ValueError, "65,535"):
            _build_mapped_replacement_mesh(original, replacement, [mapping], options)

    def test_preview_decimation_keeps_transform_responsive(self) -> None:
        original = _mesh(
            "target.pac",
            [
                SubMesh(
                    name="target",
                    material="target",
                    vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                    faces=[(0, 1, 2)],
                )
            ],
        )
        vertices = [(float(index), 0.0, 0.0) for index in range(270)]
        faces = [(index * 3, index * 3 + 1, index * 3 + 2) for index in range(90)]
        replacement = _mesh(
            "dense.obj",
            [
                SubMesh(
                    name="dense",
                    material="dense",
                    vertices=vertices,
                    faces=faces,
                )
            ],
        )
        options = StaticMeshReplacementOptions(
            transform=StaticReplacementTransform(
                alignment_mode="manual",
                scale_to_original_length=False,
                offset_xyz=(0.5, 0.0, 0.0),
            ),
            submesh_mappings=[
                StaticSubmeshMapping(
                    target_submesh_index=0,
                    target_submesh_name="target",
                    source_submesh_indices=[0],
                    target_material_slot_index=0,
                )
            ],
        )

        preview = build_static_replacement_preview_mesh(
            original,
            replacement,
            options,
            max_source_faces_per_submesh=10,
        )

        self.assertLessEqual(len(preview.submeshes[0].faces), 10)
        self.assertLessEqual(len(preview.submeshes[0].vertices), 30)
        self.assertEqual(preview.submeshes[0].vertices[0], (0.5, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
