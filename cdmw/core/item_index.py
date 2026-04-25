from __future__ import annotations

import os
import re
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from cdmw.core.archive import hashlittle, read_archive_entry_data
from cdmw.core.common import raise_if_cancelled
from cdmw.models import ArchiveEntry
from cdmw.models import RunCancelled


@dataclass(slots=True)
class ArchiveItemRecord:
    item_id: int
    internal_name: str
    display_name: str = ""
    localized_names: tuple[str, ...] = ()
    prefab_hashes: List[int] = field(default_factory=list)
    pac_files: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ArchiveItemSearchIndex:
    items: List[ArchiveItemRecord]
    pac_to_items: Dict[str, List[ArchiveItemRecord]]
    model_base_aliases: Dict[str, str]


@dataclass(slots=True)
class _ArchiveItemIndexSources:
    localization_entries: Dict[str, ArchiveEntry] = field(default_factory=dict)
    iteminfo_entry: Optional[ArchiveEntry] = None
    model_entries: List[ArchiveEntry] = field(default_factory=list)


_ITEMINFO_MARKER = b"\x00\x01\x00\x00\x00\x00\x00\x00\x00\x07\x70\x00\x00\x00"
_ITEM_INTERNAL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MODEL_HASH_SUFFIXES = ("", "_l", "_r", "_u", "_s", "_t", "_index01", "_index02", "_index03")
_LOCALIZATION_TABLES = (
    ("kor", "localizationstring_kor"),
    ("eng", "localizationstring_eng"),
    ("jpn", "localizationstring_jpn"),
    ("rus", "localizationstring_rus"),
    ("tur", "localizationstring_tur"),
    ("spa-es", "localizationstring_spa-es"),
    ("spa-mx", "localizationstring_spa-mx"),
    ("fre", "localizationstring_fre"),
    ("ger", "localizationstring_ger"),
    ("ita", "localizationstring_ita"),
    ("pol", "localizationstring_pol"),
    ("por-br", "localizationstring_por-br"),
    ("zho-tw", "localizationstring_zho-tw"),
    ("zho-cn", "localizationstring_zho-cn"),
)
_LOCALIZATION_TABLE_BY_NAME = {table_name: language_code for language_code, table_name in _LOCALIZATION_TABLES}


def _entry_package_group(entry: ArchiveEntry) -> str:
    try:
        return entry.pamt_path.parent.name.lower()
    except Exception:
        return ""


def _find_archive_entry(entries: Sequence[ArchiveEntry], package_group: str, needle: str) -> Optional[ArchiveEntry]:
    normalized_group = str(package_group or "").strip().lower()
    normalized_needle = str(needle or "").strip().lower()
    if not normalized_group or not normalized_needle:
        return None
    for entry in entries:
        if _entry_package_group(entry) != normalized_group:
            continue
        if normalized_needle in entry.path.lower():
            return entry
    return None


def _collect_archive_item_index_sources(
    entries: Sequence[ArchiveEntry],
    *,
    stop_event: Optional[threading.Event] = None,
) -> _ArchiveItemIndexSources:
    sources = _ArchiveItemIndexSources()
    for index, entry in enumerate(entries):
        if index % 4096 == 0:
            raise_if_cancelled(stop_event)
        lower_path = entry.path.lower()
        wants_localization = "localizationstring_" in lower_path
        wants_iteminfo = "iteminfo.pabgb" in lower_path
        wants_model_hash = lower_path.endswith((".prefab", ".pac", ".pact"))
        if not (wants_localization or wants_iteminfo or wants_model_hash):
            continue
        group = _entry_package_group(entry)
        if wants_localization and group == "0020":
            for table_name, language_code in _LOCALIZATION_TABLE_BY_NAME.items():
                if table_name in lower_path:
                    sources.localization_entries.setdefault(language_code, entry)
                    break
        elif wants_iteminfo and group == "0008" and sources.iteminfo_entry is None:
            sources.iteminfo_entry = entry
        elif wants_model_hash and group == "0009":
            sources.model_entries.append(entry)
    return sources


def _parse_archive_localization_entry(
    loc_entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, str]:
    data, _decompressed, _note = read_archive_entry_data(loc_entry, stop_event=stop_event)
    loc_dict: Dict[str, str] = {}
    pos = 0
    while pos + 8 < len(data):
        raise_if_cancelled(stop_event)
        slen = struct.unpack_from("<I", data, pos)[0]
        if slen == 0 or slen > 50_000 or pos + 4 + slen > len(data):
            pos += 1
            continue

        s_bytes = data[pos + 4 : pos + 4 + slen]
        if 6 <= slen <= 20 and all(0x30 <= value <= 0x39 for value in s_bytes):
            loc_id = s_bytes.decode("ascii")
            text_pos = pos + 4 + slen
            if text_pos + 4 < len(data):
                text_len = struct.unpack_from("<I", data, text_pos)[0]
                if 0 < text_len < 50_000 and text_pos + 4 + text_len <= len(data):
                    text = data[text_pos + 4 : text_pos + 4 + text_len].decode(
                        "utf-8",
                        errors="replace",
                    )
                    loc_dict[loc_id] = text
                    pos = text_pos + 4 + text_len
                    continue
        pos += 1

    return loc_dict


def parse_archive_localization_strings(
    entries: Sequence[ArchiveEntry],
    *,
    table_name: str = "localizationstring_eng",
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, str]:
    loc_entry = _find_archive_entry(entries, "0020", table_name)
    if loc_entry is None:
        if on_log is not None:
            on_log(f"Item-name search: {table_name} was not found in package 0020.")
        return {}

    return _parse_archive_localization_entry(loc_entry, stop_event=stop_event)


def _parse_archive_localization_tables_from_sources(
    sources: _ArchiveItemIndexSources,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Dict[str, str]]:
    loc_tables: Dict[str, Dict[str, str]] = {}
    missing_tables: List[str] = []
    for language_code, table_name in _LOCALIZATION_TABLES:
        raise_if_cancelled(stop_event)
        loc_entry = sources.localization_entries.get(language_code)
        if loc_entry is None:
            missing_tables.append(table_name)
            continue
        try:
            table = _parse_archive_localization_entry(loc_entry, stop_event=stop_event)
        except RunCancelled:
            raise
        except Exception as exc:
            if on_log is not None:
                on_log(f"Item-name search: skipped {table_name}: {exc}")
            continue
        if table:
            loc_tables[language_code] = table
    if missing_tables and on_log is not None:
        on_log(
            "Item-name search: "
            f"{len(missing_tables):,} localization table(s) not found in package 0020: "
            f"{', '.join(missing_tables)}."
        )
    return loc_tables


def parse_archive_localization_tables(
    entries: Sequence[ArchiveEntry],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Dict[str, str]]:
    sources = _collect_archive_item_index_sources(entries, stop_event=stop_event)
    return _parse_archive_localization_tables_from_sources(
        sources,
        on_log=on_log,
        stop_event=stop_event,
    )


def _parse_archive_iteminfo_entry(
    item_entry: ArchiveEntry,
    loc_tables: Mapping[str, Mapping[str, str]],
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[ArchiveItemRecord]:
    data, _decompressed, _note = read_archive_entry_data(item_entry, stop_event=stop_event)
    items: List[ArchiveItemRecord] = []
    seen_ids: set[int] = set()
    idx = 0
    while True:
        raise_if_cancelled(stop_event)
        pos = data.find(_ITEMINFO_MARKER, idx)
        if pos == -1:
            break
        idx = pos + len(_ITEMINFO_MARKER)
        null_pos = pos

        name_start = null_pos
        while name_start > 0 and 0x21 <= data[name_start - 1] <= 0x7E:
            name_start -= 1
            if null_pos - name_start > 150:
                break
        if null_pos - name_start < 3 or name_start < 8:
            continue

        name = data[name_start:null_pos].decode("ascii", errors="replace")
        if not _ITEM_INTERNAL_NAME_RE.match(name):
            continue
        try:
            name_len = struct.unpack_from("<I", data, name_start - 4)[0]
            item_id = struct.unpack_from("<I", data, name_start - 8)[0]
        except struct.error:
            continue
        if name_len not in (len(name), len(name) + 1):
            continue
        if item_id < 100 or item_id > 100_000_000 or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        loc_id = ""
        loc_off = pos + 18
        if loc_off + 4 < len(data):
            loc_len = struct.unpack_from("<I", data, loc_off)[0]
            if 5 < loc_len < 25 and loc_off + 4 + loc_len <= len(data):
                loc_bytes = data[loc_off + 4 : loc_off + 4 + loc_len]
                if all(0x30 <= value <= 0x39 for value in loc_bytes):
                    loc_id = loc_bytes.decode("ascii")

        prefab_hashes: List[int] = []
        search_end = min(len(data), pos + 800)
        for scan in range(pos + 14, search_end - 15):
            if data[scan] != 0x0E:
                continue
            count1 = struct.unpack_from("<I", data, scan + 3)[0]
            count2 = struct.unpack_from("<I", data, scan + 7)[0]
            if not (0 < count1 <= 5 and 0 < count2 <= 5):
                continue
            for hash_index in range(count2):
                value = struct.unpack_from("<I", data, scan + 11 + hash_index * 4)[0]
                if value:
                    prefab_hashes.append(value)
            if prefab_hashes:
                break

        localized_names: List[str] = []
        seen_names: set[str] = set()
        if loc_id:
            for _language_code, table in loc_tables.items():
                localized_name = str(table.get(loc_id, "") or "").strip()
                normalized_name = localized_name.casefold()
                if localized_name and normalized_name not in seen_names:
                    localized_names.append(localized_name)
                    seen_names.add(normalized_name)
        display_name = ""
        if loc_id:
            display_name = str(loc_tables.get("eng", {}).get(loc_id, "") or "").strip()
            if not display_name and localized_names:
                display_name = localized_names[0]

        items.append(
            ArchiveItemRecord(
                item_id=item_id,
                internal_name=name,
                display_name=display_name,
                localized_names=tuple(localized_names),
                prefab_hashes=prefab_hashes,
            )
        )

    return items


def parse_archive_iteminfo(
    entries: Sequence[ArchiveEntry],
    loc_tables: Mapping[str, Mapping[str, str]],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[ArchiveItemRecord]:
    item_entry = _find_archive_entry(entries, "0008", "iteminfo.pabgb")
    if item_entry is None:
        if on_log is not None:
            on_log("Item-name search: iteminfo.pabgb was not found in package 0008.")
        return []

    return _parse_archive_iteminfo_entry(item_entry, loc_tables, stop_event=stop_event)


def _build_archive_model_hash_table_from_entries(entries: Sequence[ArchiveEntry]) -> Dict[int, str]:
    hash_to_name: Dict[int, str] = {}
    for entry in entries:
        lower_path = entry.path.lower()
        if not lower_path.endswith((".prefab", ".pac", ".pact")):
            continue
        base = os.path.splitext(os.path.basename(lower_path))[0]
        for suffix in _MODEL_HASH_SUFFIXES:
            name = base + suffix
            hash_to_name[hashlittle(name.encode("ascii"), 0xC5EDE)] = name
    return hash_to_name


def build_archive_model_hash_table(entries: Sequence[ArchiveEntry]) -> Dict[int, str]:
    sources = _collect_archive_item_index_sources(entries)
    return _build_archive_model_hash_table_from_entries(sources.model_entries)


def build_archive_item_search_index(
    entries: Sequence[ArchiveEntry],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> ArchiveItemSearchIndex:
    try:
        sources = _collect_archive_item_index_sources(entries, stop_event=stop_event)
        loc_tables = _parse_archive_localization_tables_from_sources(
            sources,
            on_log=on_log,
            stop_event=stop_event,
        )
        if on_log is not None:
            loaded = ", ".join(f"{language}={len(table):,}" for language, table in loc_tables.items())
            on_log(f"Item-name search: loaded localization tables ({loaded or 'none'}).")
        if sources.iteminfo_entry is None:
            if on_log is not None:
                on_log("Item-name search: iteminfo.pabgb was not found in package 0008.")
            items = []
        else:
            items = _parse_archive_iteminfo_entry(
                sources.iteminfo_entry,
                loc_tables,
                stop_event=stop_event,
            )
        if on_log is not None:
            on_log(f"Item-name search: parsed {len(items):,} item database record(s).")
        hash_table = _build_archive_model_hash_table_from_entries(sources.model_entries)
        if on_log is not None:
            on_log(f"Item-name search: indexed {len(hash_table):,} model hash candidate(s).")
    except RunCancelled:
        raise

    pac_to_items: Dict[str, List[ArchiveItemRecord]] = {}
    model_base_aliases: Dict[str, str] = {}
    items_with_models: List[ArchiveItemRecord] = []

    for item in items:
        for prefab_hash in item.prefab_hashes:
            resolved = hash_table.get(prefab_hash)
            if not resolved:
                continue
            base = resolved
            for suffix in _MODEL_HASH_SUFFIXES[1:]:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            pac_name = base + ".pac"
            if pac_name not in item.pac_files:
                item.pac_files.append(pac_name)
            pac_to_items.setdefault(pac_name, []).append(item)
            terms = " ".join(
                token
                for token in (
                    item.display_name.lower(),
                    " ".join(name.lower() for name in item.localized_names),
                    item.internal_name.lower(),
                    base.lower(),
                    pac_name.lower(),
                )
                if token
            )
            if not terms:
                continue
            existing = model_base_aliases.get(base, "")
            model_base_aliases[base] = f"{existing} {terms}".strip() if existing else terms
        if item.display_name and item.pac_files:
            items_with_models.append(item)

    if on_log is not None:
        on_log(f"Item-name search: linked {len(items_with_models):,} item(s) to model asset(s).")

    return ArchiveItemSearchIndex(
        items=items_with_models,
        pac_to_items=pac_to_items,
        model_base_aliases=model_base_aliases,
    )
