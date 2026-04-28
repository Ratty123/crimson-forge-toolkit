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

    def test_manual_texture_override_combo_selection_commits_assignment(self) -> None:
        source = _main_window_source()
        self.assertIn(
            "selected_source_combo.currentIndexChanged.connect(_stage_selected_texture_source)",
            source,
        )
        self.assertIn(
            "QTimer.singleShot(0, lambda row=row_state, path=source_path: _commit_texture_row_source(row, path, sync_editor=False))",
            source,
        )
        self.assertIn(
            "def _refresh_texture_row_in_place(row_state: Dict[str, Any], *, sync_editor: bool = True)",
            source,
        )
        self.assertIn('row_state["checked"] = bool(checked and row_state["source_path"])', source)
        self.assertNotIn("selected_source_combo.activated.connect", source)
        self.assertNotIn("selected_source_combo.textActivated.connect", source)


if __name__ == "__main__":
    unittest.main()
