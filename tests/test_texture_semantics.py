from pathlib import Path
import unittest

from cdmw.core.upscale_profiles import (
    classify_texture_type,
    derive_texture_group_key,
    infer_texture_semantics,
    suggest_texture_upscale_decision,
)


class TextureSemanticPathTests(unittest.TestCase):
    def test_texture_semantic_helpers_accept_path_objects(self) -> None:
        texture_path = Path("character/texture/Imported_Normal_OpenGL.dds")

        self.assertEqual(classify_texture_type(texture_path), "normal")
        self.assertEqual(derive_texture_group_key(texture_path), "character/texture/Imported")
        self.assertEqual(
            derive_texture_group_key(Path("character/texture/Imported_Base_Color.dds")),
            "character/texture/Imported",
        )

        semantic = infer_texture_semantics(texture_path)
        self.assertEqual(semantic.path, "character/texture/Imported_Normal_OpenGL.dds")
        self.assertEqual(semantic.texture_type, "normal")

        decision = suggest_texture_upscale_decision(texture_path)
        self.assertEqual(decision.texture_type, "normal")


if __name__ == "__main__":
    unittest.main()
