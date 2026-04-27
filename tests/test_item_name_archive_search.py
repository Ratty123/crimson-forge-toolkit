from __future__ import annotations

import unittest
from pathlib import Path

from cdmw.core.archive import (
    crypt_chacha20_filename,
    filter_archive_entries,
    hashlittle,
    try_decrypt_archive_entry_data,
)
from cdmw.core.item_index import (
    ArchiveItemRecord,
    _ITEMINFO_MARKER,
    _build_archive_model_hash_table_from_entries,
    _parse_archive_iteminfo_data,
    _parse_stringinfo_model_icon_hashes_from_data,
    _strip_archive_model_variant_suffix,
)
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

    def test_archive_filter_matches_alias_after_d_variant_suffix_strip(self) -> None:
        entries = [
            _entry("character/model/cd_m0001_00_crowman_hel_0001_d.prefab"),
            _entry("character/model/cd_unrelated_hel_0001_d.prefab"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Blackwing Mask",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_m0001_00_crowman_hel_0001": (
                    "blackwing mask item_hel_blackwing cd_m0001_00_crowman_hel_0001.pac"
                ),
            },
        )

        self.assertEqual(
            [entry.path for entry in filtered],
            ["character/model/cd_m0001_00_crowman_hel_0001_d.prefab"],
        )

    def test_archive_filter_matches_item_alias_for_texture_family_suffixes(self) -> None:
        entries = [
            _entry("character/texture/cd_m0001_00_crowman_hel_0001_o.dds"),
            _entry("character/texture/cd_m0001_00_crowman_hel_0001_ma.dds"),
            _entry("character/texture/cd_unrelated_hel_0001_o.dds"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Blackwing Mask",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_m0001_00_crowman_hel_0001": (
                    "blackwing mask item_hel_blackwing cd_m0001_00_crowman_hel_0001.pac"
                ),
            },
        )

        self.assertEqual(
            [entry.path for entry in filtered],
            [
                "character/texture/cd_m0001_00_crowman_hel_0001_o.dds",
                "character/texture/cd_m0001_00_crowman_hel_0001_ma.dds",
            ],
        )

    def test_archive_filter_expands_item_alias_model_match_to_same_stem_companions(self) -> None:
        entries = [
            _entry("character/model/cd_m0001_00_carta_hel_0001.pac"),
            _entry("character/modelproperty/cd_m0001_00_carta_hel_0001.pac_xml"),
            _entry("character/model/cd_unrelated_hel_0001.pac_xml"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Carta Plate Helm",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_m0001_00_carta_hel_0001": (
                    "carta plate helm item_hel_carta cd_m0001_00_carta_hel_0001.pac"
                ),
            },
        )

        self.assertEqual(
            [entry.path for entry in filtered],
            [
                "character/model/cd_m0001_00_carta_hel_0001.pac",
                "character/modelproperty/cd_m0001_00_carta_hel_0001.pac_xml",
            ],
        )

    def test_archive_filter_expands_item_alias_prefab_helm_descriptor_to_model_family(self) -> None:
        entries = [
            _entry("character/bin/_prefab/1_pc/01/cd_phm_00_hel_0013_05_c.prefab"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac"),
            _entry("character/modelproperty/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac_xml"),
            _entry("character/texture/cd_ptm_01_hel_0013_05_n.dds"),
            _entry("character/texture/cd_phm_00_hel_0013_05_mg.dds"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0099.pac"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Canta Plate Helm",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_phm_00_hel_0013_05": "canta plate helm item_hel_canta",
            },
        )

        self.assertEqual(
            filtered[0].path,
            "character/bin/_prefab/1_pc/01/cd_phm_00_hel_0013_05_c.prefab",
        )
        self.assertCountEqual(
            [entry.path for entry in filtered[1:]],
            [
                "character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac",
                "character/modelproperty/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac_xml",
                "character/texture/cd_phm_00_hel_0013_05_mg.dds",
                "character/texture/cd_ptm_01_hel_0013_05_n.dds",
            ],
        )

    def test_archive_filter_expands_item_alias_prefab_set_helm_to_model_family(self) -> None:
        entries = [
            _entry("character/bin/_prefab/1_pc/01/cd_phm_00_hel_set_0106_c.prefab"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac"),
            _entry("character/modelproperty/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac_xml"),
            _entry("character/texture/cd_ptm_01_hel_0106_o.dds"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0107.pac"),
        ]

        filtered = filter_archive_entries(
            entries,
            filter_text="Carta Plate Helm",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_phm_00_hel_set_0106": "carta plate helm item_hel_carta",
            },
        )

        self.assertEqual(
            [entry.path for entry in filtered],
            [
                "character/bin/_prefab/1_pc/01/cd_phm_00_hel_set_0106_c.prefab",
                "character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac",
                "character/modelproperty/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac_xml",
                "character/texture/cd_ptm_01_hel_0106_o.dds",
            ],
        )

    def test_archive_filter_matches_plate_helm_model_through_prefab_descriptor_alias(self) -> None:
        entries = [
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac"),
            _entry("character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0099.pac"),
        ]

        canta_filtered = filter_archive_entries(
            entries,
            filter_text="Canta Plate Helm",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_phm_00_hel_0013_05_c": "canta plate helm item_hel_canta",
                "cd_phm_00_hel_set_0106_c": "carta plate helm item_hel_carta",
            },
        )
        carta_filtered = filter_archive_entries(
            entries,
            filter_text="Carta Plate Helm",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_phm_00_hel_0013_05_c": "canta plate helm item_hel_canta",
                "cd_phm_00_hel_set_0106_c": "carta plate helm item_hel_carta",
            },
        )

        self.assertEqual(
            [entry.path for entry in canta_filtered],
            ["character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0013_05.pac"],
        )
        self.assertEqual(
            [entry.path for entry in carta_filtered],
            ["character/model/1_pc/14_ptm/armor/13_hel/cd_ptm_01_hel_0106.pac"],
        )

    def test_model_hash_table_indexes_stripped_variant_base(self) -> None:
        table = _build_archive_model_hash_table_from_entries(
            [
                _entry("character/model/cd_m0001_00_crowman_hel_0000_c.prefab"),
                _entry("character/model/cd_m0001_00_crowman_hel_0001_d.prefab"),
                _entry("character/model/cd_m0001_00_crowman_hel_0002d.prefab"),
            ]
        )

        self.assertEqual(
            table.get(hashlittle(b"cd_m0001_00_crowman_hel_0000", 0xC5EDE)),
            "cd_m0001_00_crowman_hel_0000",
        )
        self.assertEqual(
            table.get(hashlittle(b"cd_m0001_00_crowman_hel_0001", 0xC5EDE)),
            "cd_m0001_00_crowman_hel_0001",
        )
        self.assertEqual(
            table.get(hashlittle(b"cd_m0001_00_crowman_hel_0001_d", 0xC5EDE)),
            "cd_m0001_00_crowman_hel_0001_d",
        )
        self.assertEqual(
            table.get(hashlittle(b"cd_m0001_00_crowman_hel_0002", 0xC5EDE)),
            "cd_m0001_00_crowman_hel_0002",
        )

    def test_model_hash_table_indexes_compound_index_variants(self) -> None:
        table = _build_archive_model_hash_table_from_entries(
            [_entry("character/model/cd_phm_01_sword_0166.pac")]
        )

        self.assertEqual(
            table.get(hashlittle(b"cd_phm_01_sword_0166_index01_r", 0xC5EDE)),
            "cd_phm_01_sword_0166_index01_r",
        )
        self.assertEqual(
            _strip_archive_model_variant_suffix("cd_phm_01_sword_0166_index01_r"),
            "cd_phm_01_sword_0166",
        )

    def test_archive_filter_matches_alias_after_subpart_suffix_strip(self) -> None:
        entries = [_entry("character/model/cd_phm_01_sword_0279_sub01.pac")]

        filtered = filter_archive_entries(
            entries,
            filter_text="Tree Branch",
            exclude_filter_text="",
            extension_filter="*",
            package_filter_text="",
            structure_filter="",
            role_filter="all",
            exclude_common_technical_suffixes=False,
            min_size_kb=0,
            previewable_only=False,
            item_search_aliases={
                "cd_phm_01_sword_0279": "tree branch wood_branch_01 cd_phm_01_sword_0279.pac",
            },
        )

        self.assertEqual([entry.path for entry in filtered], ["character/model/cd_phm_01_sword_0279_sub01.pac"])

    def test_stringinfo_icon_hashes_can_supply_compatible_model_stems(self) -> None:
        icon_name = b"ItemIcon_Prefab_cd_phm_01_sword_0166_index01_r"
        icon_hash = hashlittle(icon_name, 0xC5EDE)
        stringinfo_data = (
            len(icon_name).to_bytes(4, "little")
            + icon_name
            + icon_hash.to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
        )
        icon_hashes = _parse_stringinfo_model_icon_hashes_from_data(stringinfo_data)

        item_id = 1234
        internal_name = b"AbyssReward_Mysterm_OneHandSword"
        loc_id = b"4301512826159216"
        iteminfo_data = (
            item_id.to_bytes(4, "little")
            + (len(internal_name) + 1).to_bytes(4, "little")
            + internal_name
            + _ITEMINFO_MARKER
            + b"\x00" * (18 - len(_ITEMINFO_MARKER))
            + len(loc_id).to_bytes(4, "little")
            + loc_id
            + b"\x00" * 32
            + icon_hash.to_bytes(4, "little")
            + b"\x00" * 32
        )

        records = _parse_archive_iteminfo_data(
            iteminfo_data,
            {"eng": {loc_id.decode("ascii"): "Sword of the Lord"}},
            icon_model_hashes=icon_hashes,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].display_name, "Sword of the Lord")
        self.assertEqual(records[0].model_stems, ["cd_phm_01_sword_0166_index01_r"])

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
