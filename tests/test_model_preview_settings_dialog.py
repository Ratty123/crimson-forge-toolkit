import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cdmw.models import ModelPreviewRenderSettings
from cdmw.ui.model_preview_settings_dialog import ModelPreviewSettingsDialog


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class ModelPreviewSettingsDialogTests(unittest.TestCase):
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
