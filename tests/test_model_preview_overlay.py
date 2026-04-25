from __future__ import annotations

from array import array
import math
import unittest

from PySide6.QtGui import QColor, QImage

from cdmw.models import (
    MODEL_PREVIEW_ALPHA_HANDLING_MODES,
    MODEL_PREVIEW_DIFFUSE_SWIZZLE_MODES,
    MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODES,
    MODEL_PREVIEW_SAMPLER_PROBE_MODES,
    MODEL_PREVIEW_TEXTURE_PROBE_SOURCES,
    ModelPreviewData,
    ModelPreviewMesh,
    ModelPreviewRenderSettings,
    clamp_model_preview_render_settings,
)
from cdmw.ui.widgets import (
    ModelPreviewWidget,
    _BatchRenderDiagnostic,
    _FramebufferVisibilitySample,
    _ModelPreviewDrawBatch,
)


class ModelPreviewOverlayClipTests(unittest.TestCase):
    def test_keeps_visible_overlay_line_unchanged(self) -> None:
        clipped = ModelPreviewWidget._clip_preview_line(
            (-0.25, 0.0, 0.0, 1.0),
            (0.25, 0.0, 0.0, 1.0),
        )

        self.assertEqual(
            clipped,
            ((-0.25, 0.0, 0.0, 1.0), (0.25, 0.0, 0.0, 1.0)),
        )

    def test_rejects_overlay_line_fully_outside_frustum(self) -> None:
        clipped = ModelPreviewWidget._clip_preview_line(
            (2.0, 0.0, 0.0, 1.0),
            (3.0, 0.0, 0.0, 1.0),
        )

        self.assertIsNone(clipped)

    def test_clips_overlay_line_against_near_plane(self) -> None:
        clipped = ModelPreviewWidget._clip_preview_line(
            (0.0, 0.0, -2.0, 1.0),
            (0.0, 0.0, 0.0, 1.0),
        )

        self.assertIsNotNone(clipped)
        assert clipped is not None
        self.assertAlmostEqual(clipped[0][2], -1.0)
        self.assertAlmostEqual(clipped[0][3], 1.0)
        self.assertEqual(clipped[1], (0.0, 0.0, 0.0, 1.0))

    def test_clips_overlay_line_before_perspective_divide_when_it_crosses_camera(self) -> None:
        clipped = ModelPreviewWidget._clip_preview_line(
            (0.0, 0.0, 0.0, -1.0),
            (0.0, 0.0, 0.0, 1.0),
        )

        self.assertIsNotNone(clipped)
        assert clipped is not None
        self.assertGreaterEqual(clipped[0][3], ModelPreviewWidget._OVERLAY_CLIP_EPSILON)
        self.assertGreaterEqual(clipped[1][3], ModelPreviewWidget._OVERLAY_CLIP_EPSILON)


class ModelPreviewRenderSafetyTests(unittest.TestCase):
    def test_render_settings_roundtrip_new_diagnostic_controls(self) -> None:
        for mode in MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODES:
            settings = clamp_model_preview_render_settings(ModelPreviewRenderSettings(render_diagnostic_mode=mode))
            self.assertEqual(mode, settings.render_diagnostic_mode)
        settings = clamp_model_preview_render_settings(
            ModelPreviewRenderSettings(
                alpha_handling_mode=MODEL_PREVIEW_ALPHA_HANDLING_MODES[-1],
                texture_probe_source=MODEL_PREVIEW_TEXTURE_PROBE_SOURCES[-1],
                sampler_probe_mode=MODEL_PREVIEW_SAMPLER_PROBE_MODES[-1],
                diffuse_swizzle_mode=MODEL_PREVIEW_DIFFUSE_SWIZZLE_MODES[-1],
                disable_tint=True,
                disable_brightness=True,
                disable_uv_scale=True,
                force_nearest_no_mipmaps=True,
                disable_normal_map=True,
                disable_material_map=True,
                disable_height_map=True,
                disable_all_support_maps=True,
                disable_lighting=True,
                disable_depth_test=True,
                show_texture_debug_strip=True,
                solo_batch_index=3,
            )
        )
        self.assertEqual(MODEL_PREVIEW_ALPHA_HANDLING_MODES[-1], settings.alpha_handling_mode)
        self.assertEqual(MODEL_PREVIEW_TEXTURE_PROBE_SOURCES[-1], settings.texture_probe_source)
        self.assertEqual(MODEL_PREVIEW_SAMPLER_PROBE_MODES[-1], settings.sampler_probe_mode)
        self.assertEqual(MODEL_PREVIEW_DIFFUSE_SWIZZLE_MODES[-1], settings.diffuse_swizzle_mode)
        self.assertTrue(settings.disable_tint)
        self.assertTrue(settings.force_nearest_no_mipmaps)
        self.assertEqual(3, settings.solo_batch_index)

    def test_render_settings_clamp_invalid_diagnostic_controls(self) -> None:
        settings = clamp_model_preview_render_settings(
            ModelPreviewRenderSettings(
                render_diagnostic_mode="bad",
                alpha_handling_mode="bad",
                texture_probe_source="bad",
                sampler_probe_mode="bad",
                diffuse_swizzle_mode="bad",
                solo_batch_index=-22,
            )
        )
        defaults = ModelPreviewRenderSettings()
        self.assertEqual(defaults.render_diagnostic_mode, settings.render_diagnostic_mode)
        self.assertEqual(defaults.alpha_handling_mode, settings.alpha_handling_mode)
        self.assertEqual(defaults.texture_probe_source, settings.texture_probe_source)
        self.assertEqual(defaults.sampler_probe_mode, settings.sampler_probe_mode)
        self.assertEqual(defaults.diffuse_swizzle_mode, settings.diffuse_swizzle_mode)
        self.assertEqual(-1, settings.solo_batch_index)

    def test_vertex_blob_repairs_invalid_normals_and_preserves_uv_batch(self) -> None:
        mesh = ModelPreviewMesh(
            positions=[
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ],
            normals=[
                (0.0, 0.0, 0.0),
                (math.nan, 0.0, 0.0),
                (0.0, math.inf, 0.0),
            ],
            texture_coordinates=[
                (0.0, 0.0),
                (1.0, 0.0),
                (0.0, 1.0),
            ],
            indices=[0, 1, 2],
            preview_texture_path="example.png",
            preview_color=(math.nan, -1.0, 2.0),
        )
        model = ModelPreviewData(meshes=[mesh])

        vertex_blob, vertex_count, batches = ModelPreviewWidget._build_vertex_blob(model)
        values = array("f")
        values.frombytes(vertex_blob)

        self.assertEqual(3, vertex_count)
        self.assertEqual(1, len(batches))
        self.assertTrue(batches[0].has_texture_coordinates)
        self.assertGreaterEqual(batches[0].normal_repair_count, 1)
        self.assertTrue(all(math.isfinite(value) for value in values))
        first_normal = tuple(values[3:6])
        self.assertAlmostEqual(1.0, math.sqrt(sum(component * component for component in first_normal)))

    def test_texture_visibility_sampling_reports_luma_dark_ratio_and_alpha(self) -> None:
        image = QImage(2, 1, QImage.Format_RGBA8888)
        image.setPixelColor(0, 0, QColor(0, 0, 0, 128))
        image.setPixelColor(1, 0, QColor(255, 255, 255, 255))

        sample = ModelPreviewWidget._sample_base_texture_visibility(
            image,
            [(0.0, 0.0), (0.99, 0.0)],
            flip_vertical=False,
            max_samples=8,
        )

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(0.5, sample.average_luma, places=2)
        self.assertAlmostEqual(0.5, sample.dark_ratio, places=2)
        self.assertGreater(sample.average_alpha, 0.70)
        self.assertLess(sample.average_alpha, 1.0)

    def test_framebuffer_visibility_sampling_ignores_background_pixels(self) -> None:
        background = QColor(10, 10, 10)
        image = QImage(4, 4, QImage.Format_RGBA8888)
        image.fill(background)
        image.setPixelColor(1, 1, QColor(220, 220, 220))
        image.setPixelColor(2, 1, QColor(8, 8, 8))

        sample = ModelPreviewWidget._sample_framebuffer_visibility(
            image,
            background,
            max_samples=64,
        )

        self.assertGreaterEqual(sample.visible_pixels, 1)
        self.assertGreater(sample.background_ratio, 0.5)
        self.assertGreater(sample.average_luma, 0.5)

    def test_render_sampling_diagnostics_include_geometry_and_output_buckets(self) -> None:
        widget = ModelPreviewWidget.__new__(ModelPreviewWidget)
        widget._mesh_batches = [
            _ModelPreviewDrawBatch(mesh_index=0, material_name="mat", texture_name="", first_vertex=0, vertex_count=3)
        ]
        widget._batch_render_diagnostics = {
            0: _BatchRenderDiagnostic(
                batch_index=0,
                mesh_index=0,
                label="mat",
                texture_path_set=True,
                image_loaded=True,
                image_size="2x2",
                uv_valid=True,
                uv_count=3,
                position_count=3,
                texture_uploaded=True,
                use_texture=True,
                sampled_luma=0.4,
                sampled_dark_ratio=0.0,
                normal_finite_ratio=0.67,
                normal_repair_count=1,
                tangent_finite_ratio=1.0,
                bitangent_finite_ratio=1.0,
                uv_finite_ratio=1.0,
            )
        }
        widget._framebuffer_visibility_diagnostic = _FramebufferVisibilitySample(
            visible_pixels=10,
            average_luma=0.5,
            dark_ratio=0.0,
            background_ratio=0.9,
        )
        widget._render_settings = ModelPreviewRenderSettings(render_diagnostic_mode="base_color")

        text = "\n".join(widget._render_sampling_diagnostic_lines())

        self.assertIn("Diagnostic Render Mode: Base Color", text)
        self.assertIn("Framebuffer probe:", text)
        self.assertIn("normals=67% repaired=1", text)
        self.assertIn("tangent=100%", text)
        self.assertIn("final_bucket=invalid normals repaired", text)


if __name__ == "__main__":
    unittest.main()
