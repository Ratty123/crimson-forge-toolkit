from __future__ import annotations

import unittest
from pathlib import Path

from cdmw.core.archive import (
    crypt_chacha20_filename,
    filter_archive_entries,
    try_decrypt_archive_entry_data,
)
from cdmw.core.item_index import ArchiveItemRecord
from cdmw.models import ArchiveEntry


def _entry(path: str) -> ArchiveEntry:
    return ArchiveEntry(
        path=path,
        pamt_path=Path("C:/game/0009/0.pamt"),
        paz_file=Path("C:/game/0009/0.paz"),
        offset=0,
        comp_size=100,
        orig_size=100,
        flags=0,
        paz_index=0,
    )


def _encrypted_entry(path: str) -> ArchiveEntry:
    entry = _entry(path)
    entry.flags = 3 << 4
    return entry


class ItemNameArchiveSearchTests(unittest.TestCase):
    def test_paloc_binary_payload_passes_chacha20_validation(self) -> None:
        payload = (
            (b"123456", "Vow of the Dead King".encode("utf-8")),
            (b"123457", "Todtenkonigs Schwur".encode("utf-8")),
        )
        data = bytearray()
        for loc_id, text in payload:
            data.extend(len(loc_id).to_bytes(4, "little"))
            data.extend(loc_id)
            data.extend(len(text).to_bytes(4, "little"))
            data.extend(text)

        entry = _encrypted_entry("gamedata/stringtable/binary__/localizationstring_eng.paloc")
        encrypted = crypt_chacha20_filename(bytes(data), entry.basename)

        decrypted, note = try_decrypt_archive_entry_data(entry, encrypted)

        self.assertEqual(decrypted, bytes(data))
        self.assertEqual(note, "ChaCha20")

    def test_archive_filter_matches_item_display_name_alias(self) -> None:
        entries = [
            _entry("character/model/cd_weapon_king_halberd.pac"),
            _entry("character/model/cd_unrelated_sword.pac"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Vow of the Dead King",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_weapon_king_halberd": "vow of the dead king item_halberd_001 cd_weapon_king_halberd.pac",
            },
        )

        self.assertEqual([entry.path for entry in filtered], ["character/model/cd_weapon_king_halberd.pac"])

    def test_archive_filter_matches_alias_after_variant_suffix_strip(self) -> None:
        entries = [_entry("character/model/cd_weapon_king_halberd_l.pami")]

        filtered = filter_archive_entries(
            entries,
            filter_text="dead king",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_weapon_king_halberd": "vow of the dead king item_halberd_001 cd_weapon_king_halberd.pac",
            },
        )

        self.assertEqual([entry.path for entry in filtered], ["character/model/cd_weapon_king_halberd_l.pami"])

    def test_item_records_can_carry_multilingual_names(self) -> None:
        record = ArchiveItemRecord(
            item_id=1000,
            internal_name="Item_Halberd_001",
            display_name="Vow of the Dead King",
            localized_names=(
                "Vow of the Dead King",
                "Todtenkonigs Schwur",
                "誓約",
            ),
        )

        alias = " ".join(
            token
            for token in (
                record.display_name.lower(),
                " ".join(name.lower() for name in record.localized_names),
                record.internal_name.lower(),
                "cd_weapon_king_halberd",
                "cd_weapon_king_halberd.pac",
            )
            if token
        )

        filtered = filter_archive_entries(
            [_entry("character/model/cd_weapon_king_halberd.pac")],
            filter_text="todtenkonigs",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={"cd_weapon_king_halberd": alias},
        )

        self.assertEqual([entry.path for entry in filtered], ["character/model/cd_weapon_king_halberd.pac"])


if __name__ == "__main__":
    unittest.main()
