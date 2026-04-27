from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cdmw.core.archive import _find_archive_model_related_entries
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


class MeshCompanionDiscoveryTests(unittest.TestCase):
    def test_pac_related_files_include_common_companion_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _entry("character/model/weapon/test_weapon.pac", root)
            companions = [
                _entry("character/modelproperty/weapon/test_weapon.pac_xml", root),
                _entry("character/modelproperty/weapon/test_weapon.pac.xml", root),
                _entry("character/model/weapon/test_weapon.app_xml", root),
                _entry("character/model/weapon/test_weapon.prefabdata.xml", root),
                _entry("character/model/weapon/test_weapon.hkx", root),
            ]
            basename_index: dict[str, list[ArchiveEntry]] = {}
            for entry in (source, *companions):
                basename_index.setdefault(entry.basename.lower(), []).append(entry)

            related = _find_archive_model_related_entries(source, basename_index)
            related_paths = {entry.path for entry in related}

            for companion in companions:
                self.assertIn(companion.path, related_paths)


if __name__ == "__main__":
    unittest.main()
