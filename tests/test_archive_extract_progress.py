from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cdmw.core.archive import extract_archive_entries
from cdmw.models import ArchiveEntry


ROOT = Path(__file__).resolve().parents[1]
MAIN_WINDOW = ROOT / "cdmw" / "ui" / "main_window.py"


def _entry(path: str, pamt_path: Path, paz_path: Path, offset: int, data: bytes) -> ArchiveEntry:
    return ArchiveEntry(
        path=path,
        pamt_path=pamt_path,
        paz_file=paz_path,
        offset=offset,
        comp_size=len(data),
        orig_size=len(data),
        flags=0,
        paz_index=0,
    )


class ArchiveExtractProgressTests(unittest.TestCase):
    def test_extract_archive_entries_emits_determinate_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pamt_path = root / "0.pamt"
            paz_path = root / "0.paz"
            first = b"alpha"
            second = b"bravo"
            pamt_path.write_bytes(b"pamt")
            paz_path.write_bytes(first + second)
            entries = [
                _entry("object/a.bin", pamt_path, paz_path, 0, first),
                _entry("object/b.bin", pamt_path, paz_path, len(first), second),
            ]
            progress: list[tuple[int, int, str]] = []

            stats = extract_archive_entries(
                entries,
                root / "out",
                on_progress=lambda current, total, detail: progress.append((current, total, detail)),
            )

            self.assertEqual(stats["extracted"], 2)
            self.assertGreaterEqual(len(progress), 3)
            self.assertEqual(progress[0][0:2], (0, 2))
            self.assertEqual(progress[-1][0:2], (2, 2))
            self.assertIn("complete", progress[-1][2].lower())

    def test_archive_extract_utility_task_is_wired_to_archive_progress(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("progress_changed = Signal(int, int, str)", source)
        self.assertIn("worker.progress_changed.connect(self._handle_utility_progress_changed)", source)
        self.assertIn("def _handle_utility_progress_changed", source)
        self.assertIn('"] EXTRACT " in message or "] FAIL " in message', source)
        self.assertIn("on_progress=on_progress", source)
        self.assertIn("show_archive_progress=True", source)
        self.assertIn("task_accepts_progress=True", source)


if __name__ == "__main__":
    unittest.main()
