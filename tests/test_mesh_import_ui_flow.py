import unittest
from pathlib import Path

from cdmw.ui.main_window import mesh_import_mode_availability


class MeshImportUiFlowTests(unittest.TestCase):
    def test_glb_and_gltf_disable_roundtrip_and_default_to_replacement(self) -> None:
        for name in ("model.glb", "model.gltf"):
            availability = mesh_import_mode_availability(Path(name), has_roundtrip_sidecar=False, static_supported=True)

            self.assertFalse(availability.roundtrip_enabled)
            self.assertTrue(availability.static_enabled)
            self.assertEqual(availability.default_mode, "static_replacement")
            self.assertIn("static Mesh Replacement", availability.guidance)

    def test_obj_with_sidecar_defaults_to_roundtrip(self) -> None:
        availability = mesh_import_mode_availability(Path("model.obj"), has_roundtrip_sidecar=True, static_supported=True)

        self.assertTrue(availability.roundtrip_enabled)
        self.assertTrue(availability.static_enabled)
        self.assertEqual(availability.default_mode, "roundtrip")

    def test_obj_without_sidecar_defaults_to_replacement(self) -> None:
        availability = mesh_import_mode_availability(Path("model.obj"), has_roundtrip_sidecar=False, static_supported=True)

        self.assertTrue(availability.roundtrip_enabled)
        self.assertEqual(availability.default_mode, "static_replacement")

    def test_blocked_static_target_leaves_gltf_without_available_mode(self) -> None:
        availability = mesh_import_mode_availability(Path("model.glb"), has_roundtrip_sidecar=False, static_supported=False)

        self.assertFalse(availability.roundtrip_enabled)
        self.assertFalse(availability.static_enabled)
        self.assertEqual(availability.default_mode, "")


if __name__ == "__main__":
    unittest.main()
