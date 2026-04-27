from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cdmw.core.mesh_baseline import MeshBaselineCache, read_archive_entry_baseline_data
from cdmw.models import ArchiveEntry


def _entry(path: str, root: Path) -> ArchiveEntry:
    package_root = root / "0009"
    package_root.mkdir(parents=True, exist_ok=True)
    return ArchiveEntry(
        path=path,
        pamt_path=package_root / "0.pamt",
        paz_file=package_root / "0.paz",
        offset=0,
        comp_size=0,
        orig_size=0,
        flags=0,
        paz_index=0,
    )


class MeshBaselineCacheTests(unittest.TestCase):
    def test_cached_original_bytes_are_reused_after_current_archive_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _entry("character/model/example.pac", root)
            cache = MeshBaselineCache(root / "cache")
            reads = [b"original pac bytes", b"modified pac bytes"]

            def read_current(_entry: ArchiveEntry) -> tuple[bytes, object, object]:
                return reads.pop(0), False, ""

            first = read_archive_entry_baseline_data(entry, cache=cache, read_entry_data=read_current)
            second = read_archive_entry_baseline_data(entry, cache=cache, read_entry_data=read_current)

            self.assertEqual(first.data, b"original pac bytes")
            self.assertFalse(first.from_cache)
            self.assertEqual(second.data, b"original pac bytes")
            self.assertTrue(second.from_cache)
            self.assertEqual(reads, [b"modified pac bytes"])


if __name__ == "__main__":
    unittest.main()
