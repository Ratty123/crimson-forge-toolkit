import unittest

from cdmw.core.archive_modding import build_hkx_preview


class HkxPreviewTests(unittest.TestCase):
    def test_modern_tagfile_preview_reports_sdk_and_embedded_hknp_types(self) -> None:
        body = (
            b"TAG0@\x00\x00\x10SDKV20240200@\x00\x18\xf8DATA\x00"
            b"TST1hknpCompoundShape\x00"
            b"hknpShapeInstance\x00"
            b"hkcdSimdTreeNamespace::Node\x00"
            b"hkpPhysicsData\x00"
            b"character/bin/body.hkx\x00"
        )
        data = (len(body) + 4).to_bytes(4, "big") + body

        preview = build_hkx_preview(data, "character/bin/body.hkx")

        self.assertIn("Havok SDK version: 20240200 (2024.2.0)", preview.preview_text)
        self.assertIn("Detected tag sections: TAG0, SDKV, DATA", preview.preview_text)
        self.assertIn("Modern Havok Physics", preview.preview_text)
        self.assertIn("hknpCompoundShape", preview.preview_text)
        self.assertIn("hkcdSimdTreeNamespace::Node", preview.preview_text)
        self.assertIn("character/bin/body.hkx", preview.preview_text)
        self.assertTrue(any("modern Havok Physics" in line for line in preview.detail_lines))


if __name__ == "__main__":
    unittest.main()
