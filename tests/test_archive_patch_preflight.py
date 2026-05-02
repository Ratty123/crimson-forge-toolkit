import struct
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from cdmw.core.archive_modding import (
    ArchivePatchRequest,
    _calculate_pa_checksum,
    _verify_crc_chain,
    patch_archive_entries,
)
from cdmw.models import ArchiveEntry


def _name_record(name: str, parent_offset: int = 0xFFFFFFFF) -> bytes:
    raw = name.encode("utf-8")
    if len(raw) > 255:
        raise ValueError("test name record is too long")
    return struct.pack("<IB", parent_offset, len(raw)) + raw


def _write_test_archive(
    root: Path,
    virtual_path: str = "character/model/test_asset.pac",
    *,
    payload: bytes = b"old-payload",
    flags: int = 0,
    paz_index: int = 0,
    valid_pamt_crc: bool = True,
    valid_papgt_crc: bool = True,
) -> ArchiveEntry:
    package_root = root / "0009"
    meta_root = root / "meta"
    package_root.mkdir(parents=True, exist_ok=True)
    meta_root.mkdir(parents=True, exist_ok=True)

    paz_path = package_root / f"{paz_index}.paz"
    paz_path.write_bytes(payload)

    path = PurePosixPath(virtual_path.replace("\\", "/"))
    directory = path.parent.as_posix()
    basename = path.name
    directory_block = _name_record(directory)
    file_name_block = _name_record(basename)
    folder_table = struct.pack("<IIII", 0, 0, 0, 1)
    file_table = struct.pack("<IIIIHH", 0, 0, len(payload), len(payload), paz_index, flags)

    pamt_path = package_root / "0.pamt"
    pamt_raw = bytearray()
    pamt_raw.extend(struct.pack("<III", 0, paz_index + 1, 0))
    for index in range(paz_index + 1):
        if index == paz_index:
            pamt_raw.extend(struct.pack("<III", _calculate_pa_checksum(payload), len(payload), 0))
        else:
            pamt_raw.extend(struct.pack("<III", 0, 0, 0))
    pamt_raw.extend(struct.pack("<I", len(directory_block)))
    pamt_raw.extend(directory_block)
    pamt_raw.extend(struct.pack("<I", len(file_name_block)))
    pamt_raw.extend(file_name_block)
    pamt_raw.extend(struct.pack("<I", 1))
    pamt_raw.extend(folder_table)
    pamt_raw.extend(struct.pack("<I", 1))
    pamt_raw.extend(file_table)
    pamt_crc = _calculate_pa_checksum(bytes(pamt_raw[12:]))
    struct.pack_into("<I", pamt_raw, 0, pamt_crc if valid_pamt_crc else pamt_crc ^ 0xFFFFFFFF)
    pamt_path.write_bytes(pamt_raw)

    papgt_path = meta_root / "0.papgt"
    papgt_raw = bytearray(24)
    struct.pack_into("<I", papgt_raw, 20, pamt_crc)
    papgt_crc = _calculate_pa_checksum(bytes(papgt_raw[12:]))
    struct.pack_into("<I", papgt_raw, 4, papgt_crc if valid_papgt_crc else papgt_crc ^ 0xFFFFFFFF)
    papgt_path.write_bytes(papgt_raw)

    return ArchiveEntry(
        path=virtual_path,
        pamt_path=pamt_path,
        paz_file=paz_path,
        offset=0,
        comp_size=len(payload),
        orig_size=len(payload),
        flags=flags,
        paz_index=paz_index,
    )


class ArchivePatchPreflightTests(unittest.TestCase):
    def test_valid_patch_updates_checksum_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root)

            result = patch_archive_entries((ArchivePatchRequest(entry, b"new-payload"),))

            normalized_path = entry.path.lower()
            self.assertIn(normalized_path, result.changed_entries)
            changed = result.changed_entries[normalized_path]
            self.assertGreater(changed.offset, 0)
            self.assertEqual(changed.comp_size, len(b"new-payload"))
            self.assertEqual(changed.orig_size, len(b"new-payload"))
            _verify_crc_chain(root / "meta" / "0.papgt", (entry.pamt_path,))

    def test_missing_target_entry_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root, "character/model/existing.pac")
            stale_entry = ArchiveEntry(
                path="character/model/missing.pac",
                pamt_path=entry.pamt_path,
                paz_file=entry.paz_file,
                offset=entry.offset,
                comp_size=entry.comp_size,
                orig_size=entry.orig_size,
                flags=entry.flags,
                paz_index=entry.paz_index,
            )
            original_paz = entry.paz_file.read_bytes()

            with patch("cdmw.core.archive_modding._create_backup") as create_backup:
                with self.assertRaisesRegex(ValueError, "Could not locate"):
                    patch_archive_entries((ArchivePatchRequest(stale_entry, b"new-payload"),))

            create_backup.assert_not_called()
            self.assertEqual(entry.paz_file.read_bytes(), original_paz)

    def test_bad_preexisting_checksum_chain_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root, valid_pamt_crc=False)
            original_paz = entry.paz_file.read_bytes()

            with patch("cdmw.core.archive_modding._create_backup") as create_backup:
                with self.assertRaisesRegex(ValueError, "PAMT checksum verification failed"):
                    patch_archive_entries((ArchivePatchRequest(entry, b"new-payload"),))

            create_backup.assert_not_called()
            self.assertEqual(entry.paz_file.read_bytes(), original_paz)

    def test_unsupported_compression_mode_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root, flags=9)
            original_paz = entry.paz_file.read_bytes()

            with patch("cdmw.core.archive_modding._create_backup") as create_backup:
                with self.assertRaisesRegex(ValueError, "does not support compression type 9"):
                    patch_archive_entries((ArchivePatchRequest(entry, b"new-payload"),))

            create_backup.assert_not_called()
            self.assertEqual(entry.paz_file.read_bytes(), original_paz)

    def test_mismatched_paz_index_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root)
            mismatched_entry = ArchiveEntry(
                path=entry.path,
                pamt_path=entry.pamt_path,
                paz_file=entry.pamt_path.parent / "1.paz",
                offset=entry.offset,
                comp_size=entry.comp_size,
                orig_size=entry.orig_size,
                flags=entry.flags,
                paz_index=1,
            )
            original_paz = entry.paz_file.read_bytes()

            with patch("cdmw.core.archive_modding._create_backup") as create_backup:
                with self.assertRaisesRegex(ValueError, "no longer points at PAZ index 1"):
                    patch_archive_entries((ArchivePatchRequest(mismatched_entry, b"new-payload"),))

            create_backup.assert_not_called()
            self.assertEqual(entry.paz_file.read_bytes(), original_paz)

    def test_write_failure_restores_backup_and_append_only_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            entry = _write_test_archive(root)
            original_paz = entry.paz_file.read_bytes()

            with patch("cdmw.core.archive_modding._write_bytes_preserve_timestamps", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    patch_archive_entries((ArchivePatchRequest(entry, b"new-payload"),))

            self.assertEqual(entry.paz_file.read_bytes(), original_paz)
            _verify_crc_chain(root / "meta" / "0.papgt", (entry.pamt_path,))


if __name__ == "__main__":
    unittest.main()
