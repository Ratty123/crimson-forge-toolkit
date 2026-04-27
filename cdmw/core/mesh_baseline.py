from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cdmw.constants import APP_NAME
from cdmw.models import ArchiveEntry


@dataclass(slots=True)
class MeshBaselineData:
    data: bytes
    from_cache: bool
    cache_path: Optional[Path] = None
    message: str = ""


def _normalize_virtual_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lower()


def _default_baseline_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME / "mesh_baselines"
    return Path(tempfile.gettempdir()) / APP_NAME / "mesh_baselines"


def _entry_cache_key(entry: ArchiveEntry) -> str:
    package_group = str(getattr(entry.pamt_path.parent, "name", "") or "").strip().lower()
    key_text = f"{package_group}|{_normalize_virtual_path(entry.path)}"
    return hashlib.sha1(key_text.encode("utf-8", errors="ignore")).hexdigest()


class MeshBaselineCache:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or _default_baseline_root()).expanduser()

    def _paths_for_entry(self, entry: ArchiveEntry) -> tuple[Path, Path]:
        key = _entry_cache_key(entry)
        folder = self.root / key[:2]
        return folder / f"{key}.bin", folder / f"{key}.json"

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp_path.write_bytes(data)
        temp_path.replace(path)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def get(self, entry: ArchiveEntry) -> MeshBaselineData | None:
        payload_path, metadata_path = self._paths_for_entry(entry)
        if not payload_path.is_file():
            return None
        try:
            data = payload_path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        message = "Using cached original mesh donor bytes."
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            byte_count = int(metadata.get("byte_count", 0) or 0)
            sha256 = str(metadata.get("sha256", "") or "")
            if byte_count and byte_count != len(data):
                return None
            if sha256 and hashlib.sha256(data).hexdigest() != sha256:
                return None
        except Exception:
            return None
        return MeshBaselineData(data=data, from_cache=True, cache_path=payload_path, message=message)

    def snapshot(self, entry: ArchiveEntry, data: bytes) -> MeshBaselineData:
        payload_path, metadata_path = self._paths_for_entry(entry)
        payload = bytes(data or b"")
        metadata = {
            "format": "mesh_baseline_v1",
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "path": str(entry.path or ""),
            "package_group": str(getattr(entry.pamt_path.parent, "name", "") or ""),
            "pamt_path": str(entry.pamt_path),
            "paz_file": str(entry.paz_file),
            "offset": int(entry.offset),
            "compressed_size": int(entry.comp_size),
            "original_size": int(entry.orig_size),
            "flags": int(entry.flags),
            "byte_count": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        self._atomic_write_bytes(payload_path, payload)
        self._atomic_write_text(metadata_path, json.dumps(metadata, indent=2, sort_keys=True))
        return MeshBaselineData(
            data=payload,
            from_cache=False,
            cache_path=payload_path,
            message="Captured original mesh donor bytes in the baseline cache.",
        )

    def get_or_snapshot(
        self,
        entry: ArchiveEntry,
        read_entry_data: Callable[[ArchiveEntry], tuple[bytes, object, object]],
    ) -> MeshBaselineData:
        cached = self.get(entry)
        if cached is not None:
            return cached
        data, _decompressed, _note = read_entry_data(entry)
        try:
            return self.snapshot(entry, data)
        except Exception as exc:
            return MeshBaselineData(
                data=data,
                from_cache=False,
                cache_path=None,
                message=f"Baseline cache unavailable; using current archive bytes ({exc}).",
            )


def read_archive_entry_baseline_data(
    entry: ArchiveEntry,
    *,
    cache: Optional[MeshBaselineCache] = None,
    read_entry_data: Optional[Callable[[ArchiveEntry], tuple[bytes, object, object]]] = None,
) -> MeshBaselineData:
    if read_entry_data is None:
        from cdmw.core.archive import read_archive_entry_data as read_entry_data_func
    else:
        read_entry_data_func = read_entry_data
    return (cache or MeshBaselineCache()).get_or_snapshot(entry, read_entry_data_func)

