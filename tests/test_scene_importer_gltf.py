import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

from cdmw.modding.mesh_parser import ParsedMesh, SubMesh
from cdmw.modding.scene_importer import (
    SCENE_IMPORT_EXTENSIONS,
    discover_scene_texture_files,
    import_scene_mesh,
    import_scene_mesh_with_report,
)
from cdmw.modding.static_mesh_replacer import suggest_static_submesh_mappings


def _pad4(data: bytes) -> bytes:
    return data + (b"\x00" * ((4 - (len(data) % 4)) % 4))


def _triangle_payload(*, image_bytes: bytes = b"", image_mime: str = "image/png") -> tuple[bytes, dict]:
    chunks: list[bytes] = []
    buffer_views: list[dict] = []

    def add_view(data: bytes, target: int = 0) -> int:
        offset = sum(len(chunk) for chunk in chunks)
        padded = _pad4(data)
        chunks.append(padded)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target:
            view["target"] = target
        buffer_views.append(view)
        return len(buffer_views) - 1

    position_view = add_view(struct.pack("<9f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0), 34962)
    normal_view = add_view(struct.pack("<9f", 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0), 34962)
    uv_view = add_view(struct.pack("<6f", 0.0, 0.0, 1.0, 0.0, 0.0, 1.0), 34962)
    index_view = add_view(struct.pack("<3H", 0, 1, 2), 34963)
    image_view = add_view(image_bytes) if image_bytes else -1
    accessors = [
        {"bufferView": position_view, "componentType": 5126, "count": 3, "type": "VEC3"},
        {"bufferView": normal_view, "componentType": 5126, "count": 3, "type": "VEC3"},
        {"bufferView": uv_view, "componentType": 5126, "count": 3, "type": "VEC2"},
        {"bufferView": index_view, "componentType": 5123, "count": 3, "type": "SCALAR"},
    ]
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": sum(len(chunk) for chunk in chunks)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
        "materials": [{"name": "Body"}],
        "meshes": [
            {
                "name": "Triangle",
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                        "indices": 3,
                        "material": 0,
                    }
                ],
            }
        ],
        "nodes": [{"name": "Node", "mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    if image_view >= 0:
        document["materials"][0]["pbrMetallicRoughness"] = {"baseColorTexture": {"index": 0}}
        document["textures"] = [{"source": 0}]
        document["images"] = [{"bufferView": image_view, "mimeType": image_mime}]
    return b"".join(chunks), document


def _write_glb(path: Path, document: dict, bin_chunk: bytes) -> None:
    json_chunk = _pad4(json.dumps(document, separators=(",", ":")).encode("utf-8"))
    bin_payload = _pad4(bin_chunk)
    total_length = 12 + 8 + len(json_chunk) + 8 + len(bin_payload)
    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, total_length)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(bin_payload), 0x004E4942)
        + bin_payload
    )


class GltfSceneImporterTests(unittest.TestCase):
    def test_minimal_glb_triangle_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_chunk, document = _triangle_payload()
            path = Path(tmp) / "triangle.glb"
            _write_glb(path, document, bin_chunk)

            result = import_scene_mesh_with_report(path)

            self.assertIn(".glb", SCENE_IMPORT_EXTENSIONS)
            self.assertEqual(result.mesh.format, "glb")
            self.assertEqual(result.mesh.total_vertices, 3)
            self.assertEqual(result.mesh.total_faces, 1)
            self.assertTrue(result.mesh.has_uvs)

    def test_gltf_external_buffer_and_texture_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_chunk, document = _triangle_payload()
            (root / "triangle.bin").write_bytes(bin_chunk)
            (root / "body_base.png").write_bytes(b"png")
            document["buffers"][0]["uri"] = "triangle.bin"
            document["materials"][0]["pbrMetallicRoughness"] = {"baseColorTexture": {"index": 0}}
            document["textures"] = [{"source": 0}]
            document["images"] = [{"uri": "body_base.png"}]
            path = root / "triangle.gltf"
            path.write_text(json.dumps(document), encoding="utf-8")

            result = import_scene_mesh_with_report(path)
            discovered = discover_scene_texture_files(path, result.mesh)

            self.assertEqual(result.mesh.format, "gltf")
            self.assertIn((root / "body_base.png").resolve(), result.discovered_texture_files)
            self.assertIn((root / "body_base.png").resolve(), discovered)

    def test_gltf_data_uri_buffer_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_chunk, document = _triangle_payload()
            document["buffers"][0]["uri"] = "data:application/octet-stream;base64," + base64.b64encode(bin_chunk).decode("ascii")
            path = Path(tmp) / "triangle.gltf"
            path.write_text(json.dumps(document), encoding="utf-8")

            mesh = import_scene_mesh(path)

            self.assertEqual(mesh.total_faces, 1)
            self.assertEqual(mesh.submeshes[0].material, "Body")

    def test_gltf_node_transform_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_chunk, document = _triangle_payload()
            document["buffers"][0]["uri"] = "triangle.bin"
            document["nodes"][0]["translation"] = [1.0, 2.0, 3.0]
            root = Path(tmp)
            (root / "triangle.bin").write_bytes(bin_chunk)
            path = root / "triangle.gltf"
            path.write_text(json.dumps(document), encoding="utf-8")

            mesh = import_scene_mesh(path)

            self.assertEqual(mesh.bbox_min, (1.0, 2.0, 3.0))
            self.assertEqual(mesh.bbox_max, (2.0, 3.0, 3.0))

    def test_glb_embedded_image_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png_bytes = b"\x89PNG\r\n\x1a\nfake"
            bin_chunk, document = _triangle_payload(image_bytes=png_bytes)
            path = Path(tmp) / "embedded.glb"
            _write_glb(path, document, bin_chunk)

            result = import_scene_mesh_with_report(path)

            self.assertEqual(len(result.extracted_embedded_files), 1)
            self.assertTrue(result.extracted_embedded_files[0].is_file())
            self.assertEqual(result.extracted_embedded_files[0].read_bytes(), png_bytes)

    def test_compressed_gltf_is_rejected_with_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compressed.gltf"
            path.write_text(
                json.dumps({"asset": {"version": "2.0"}, "extensionsUsed": ["KHR_draco_mesh_compression"]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Export an uncompressed GLB/glTF"):
                import_scene_mesh_with_report(path)

    def test_static_mapping_accepts_imported_gltf_mesh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_chunk, document = _triangle_payload()
            document["buffers"][0]["uri"] = "triangle.bin"
            root = Path(tmp)
            (root / "triangle.bin").write_bytes(bin_chunk)
            path = root / "triangle.gltf"
            path.write_text(json.dumps(document), encoding="utf-8")
            replacement = import_scene_mesh(path)
            original = ParsedMesh(
                path="original.pam",
                format="pam",
                submeshes=[SubMesh(name="Body", material="Body", vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)], faces=[(0, 1, 2)])],
                total_vertices=3,
                total_faces=1,
                has_uvs=False,
            )

            mappings = suggest_static_submesh_mappings(original, replacement)

            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0].source_submesh_indices, [0])


if __name__ == "__main__":
    unittest.main()
