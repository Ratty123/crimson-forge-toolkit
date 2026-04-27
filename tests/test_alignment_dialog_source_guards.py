from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_WINDOW = ROOT / "cdmw" / "ui" / "main_window.py"


def _main_window_source() -> str:
    return MAIN_WINDOW.read_text(encoding="utf-8")


class AlignmentDialogSourceGuardTests(unittest.TestCase):
    def test_alignment_dialog_qsize_runtime_dependency_is_imported(self) -> None:
        source = _main_window_source()
        self.assertIn("QSize(", source)
        self.assertIn("QSettings, QSize, Qt", source)

    def test_orientation_presets_are_explicit_apply_only(self) -> None:
        source = _main_window_source()
        self.assertIn("apply_orientation_preset_button.clicked.connect", source)
        self.assertNotIn("orientation_preset_combo.currentIndexChanged.connect(_apply_orientation_preset)", source)
        self.assertIn("reset_placement_button.clicked.connect(_reset_placement_values)", source)


if __name__ == "__main__":
    unittest.main()
