import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QTreeWidget

from cdmw.ui.widgets import (
    make_tree_columns_persistent,
    persistent_tree_column_order_key,
    persistent_tree_column_widths_key,
    restore_persistent_tree_column_order,
    restore_persistent_tree_column_widths,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _settings(path: Path) -> QSettings:
    return QSettings(str(path), QSettings.Format.IniFormat)


def _tree(labels: tuple[str, ...] = ("A", "B", "C")) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setHeaderLabels(list(labels))
    return tree


class PersistentTreeHeaderTests(unittest.TestCase):
    def test_width_restore_still_works(self) -> None:
        _app()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir) / "settings.ini")
            settings.setValue(persistent_tree_column_widths_key("tree"), "140,160,180")
            tree = _tree()

            self.assertTrue(restore_persistent_tree_column_widths(tree, settings, "tree"))

            self.assertEqual(140, tree.header().sectionSize(0))
            self.assertEqual(160, tree.header().sectionSize(1))
            self.assertEqual(180, tree.header().sectionSize(2))

    def test_moved_column_order_saves_and_restores(self) -> None:
        _app()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.ini"
            settings = _settings(settings_path)
            tree = _tree()
            make_tree_columns_persistent(tree, settings, "tree", restore_later=False)
            tree.header().moveSection(2, 0)
            settings.sync()

            restored_settings = _settings(settings_path)
            restored = _tree()
            make_tree_columns_persistent(restored, restored_settings, "tree", restore_later=False)

            self.assertEqual(2, restored.header().logicalIndex(0))
            self.assertEqual(0, restored.header().logicalIndex(1))
            self.assertEqual(1, restored.header().logicalIndex(2))

    def test_stale_saved_layouts_are_ignored_when_column_count_changes(self) -> None:
        _app()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir) / "settings.ini")
            settings.setValue(persistent_tree_column_widths_key("tree"), "120,130,140")
            settings.setValue(persistent_tree_column_order_key("tree"), "2,0,1")
            tree = _tree(("A", "B"))

            self.assertFalse(restore_persistent_tree_column_widths(tree, settings, "tree"))
            self.assertFalse(restore_persistent_tree_column_order(tree, settings, "tree"))
            self.assertEqual(0, tree.header().logicalIndex(0))
            self.assertEqual(1, tree.header().logicalIndex(1))


if __name__ == "__main__":
    unittest.main()
