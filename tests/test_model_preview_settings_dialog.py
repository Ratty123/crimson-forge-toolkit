import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from cdmw.models import ModelPreviewRenderSettings
from cdmw.ui.model_preview_settings_dialog import ModelPreviewSettingsDialog


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class ModelPreviewSettingsDialogTests(unittest.TestCase):
    def test_rich_lit_mode_is_available_in_settings_dialog(self) -> None:
        _app()
        dialog = ModelPreviewSettingsDialog(settings=ModelPreviewRenderSettings(render_diagnostic_mode="rich_lit"))

        rich_index = dialog.render_diagnostic_mode_combo.findData("rich_lit")
        self.assertGreaterEqual(rich_index, 0)
        self.assertEqual("Enhanced Relief Preview", dialog.render_diagnostic_mode_combo.itemText(rich_index))
        self.assertEqual("rich_lit", dialog.current_settings().render_diagnostic_mode)

        dialog.close()
        dialog.deleteLater()

    def test_enhanced_relief_sliders_are_not_shown_in_settings_dialog(self) -> None:
        _app()
        dialog = ModelPreviewSettingsDialog(settings=ModelPreviewRenderSettings(render_diagnostic_mode="lit"))

        self.assertNotIn("height_effect_max", dialog._slider_controls)
        self.assertNotIn("specular_max", dialog._slider_controls)
        self.assertNotIn("shininess_max", dialog._slider_controls)
        self.assertEqual(
            ModelPreviewRenderSettings().height_effect_max,
            dialog.current_settings().height_effect_max,
        )

        dialog.close()
        dialog.deleteLater()

    def test_settings_dialog_warns_that_diagnostics_are_advanced(self) -> None:
        _app()
        dialog = ModelPreviewSettingsDialog(settings=ModelPreviewRenderSettings())

        dialog_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn("Advanced diagnostics", dialog_text)
        self.assertIn("no visible effect", dialog_text)

        dialog.close()
        dialog.deleteLater()

    def test_probe_texture_selection_switches_to_selected_texture_probe_mode(self) -> None:
        _app()
        dialog = ModelPreviewSettingsDialog(settings=ModelPreviewRenderSettings(render_diagnostic_mode="lit"))

        self.assertTrue(dialog.texture_probe_source_combo.isEnabled())
        self.assertEqual("lit", dialog.current_settings().render_diagnostic_mode)

        material_index = dialog.texture_probe_source_combo.findData("material")
        self.assertGreaterEqual(material_index, 0)
        dialog.texture_probe_source_combo.setCurrentIndex(material_index)

        current = dialog.current_settings()
        self.assertEqual("texture_probe", current.render_diagnostic_mode)
        self.assertEqual("material", current.texture_probe_source)

        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
