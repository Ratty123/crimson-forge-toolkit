from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cdmw.modding.mesh_importer import _choose_pac_donor_indices, import_obj
from cdmw.modding.mesh_parser import SubMesh


class MeshImportRegressionTests(unittest.TestCase):
    def test_obj_roundtrip_vertex_split_preserves_source_vertex_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            obj_path = Path(temp_dir) / "split.obj"
            obj_path.write_text(
                "\n".join(
                    [
                        "# source_path: character/model/example.pac",
                        "# source_format: pac",
                        "o Part",
                        "usemtl Mat",
                        "v 0 0 0",
                        "v 1 0 0",
                        "v 1 1 0",
                        "v 0 1 0",
                        "vt 0 0",
                        "vt 1 0",
                        "vt 1 1",
                        "vt 0.25 0.75",
                        "vt 0 1",
                        "vn 0 0 1",
                        "f 1/1/1 2/2/1 3/3/1",
                        "f 1/4/1 3/3/1 4/5/1",
                    ]
                ),
                encoding="utf-8",
            )
            Path(f"{obj_path}.meta.json").write_text(
                json.dumps(
                    {
                        "format": "mesh_roundtrip_manifest_v2",
                        "source_path": "character/model/example.pac",
                        "source_format": "pac",
                        "submeshes": [
                            {
                                "index": 0,
                                "name": "Part",
                                "material": "Mat",
                                "texture": "part.dds",
                                "vertex_count": 4,
                                "face_count": 2,
                                "source_vertex_map": [10, 11, 12, 13],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            mesh = import_obj(str(obj_path))
            submesh = mesh.submeshes[0]

            self.assertEqual(len(submesh.vertices), 5)
            self.assertEqual(submesh.source_vertex_map, [10, 11, 12, 13, 10])

    def test_pac_donor_mapping_prefers_roundtrip_source_map_for_skinning_records(self) -> None:
        original = SubMesh(
            vertices=[(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (20.0, 0.0, 0.0)],
        )
        imported = SubMesh(
            vertices=[(99.0, 99.0, 99.0), (0.0, 0.0, 0.0)],
            source_vertex_map=[2, 1],
        )

        self.assertEqual(_choose_pac_donor_indices(original, imported), [2, 1])


if __name__ == "__main__":
    unittest.main()
