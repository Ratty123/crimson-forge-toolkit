from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence


@dataclass(slots=True)
class RegisteredTextureClassification:
    path: str
    texture_type: str
    semantic_subtype: str
    source: str = "user"
    updated_at: str = ""
    note: str = ""


_registry_lock = threading.RLock()
_registry_path: Optional[Path] = None
_registry_entries: Dict[str, RegisteredTextureClassification] = {}
_registry_loaded = False


def normalize_texture_registry_key(path_value: str) -> str:
    return path_value.strip().replace("\\", "/").strip("/").casefold()


_PACKAGE_PREFIX_RE = re.compile(r"^\d{4}$")


def _candidate_texture_registry_keys(path_value: str) -> List[str]:
    normalized = str(path_value or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return []
    keys: List[str] = [normalize_texture_registry_key(normalized)]
    parts = normalized.split("/")
    if len(parts) > 1 and _PACKAGE_PREFIX_RE.fullmatch(parts[0] or ""):
        stripped = "/".join(parts[1:]).strip("/")
        if stripped:
            stripped_key = normalize_texture_registry_key(stripped)
            if stripped_key not in keys:
                keys.append(stripped_key)
    return keys


def configure_texture_classification_registry(registry_path: Path) -> None:
    global _registry_path, _registry_loaded, _registry_entries
    with _registry_lock:
        resolved_path = Path(registry_path).expanduser().resolve()
        if _registry_path == resolved_path and _registry_loaded:
            return
        _registry_path = resolved_path
        _registry_entries = {}
        _registry_loaded = False
        _ensure_registry_loaded()


def texture_classification_registry_path() -> Optional[Path]:
    with _registry_lock:
        return _registry_path


def get_registered_texture_classification(path_value: str) -> Optional[RegisteredTextureClassification]:
    with _registry_lock:
        _ensure_registry_loaded()
        for key in _candidate_texture_registry_keys(path_value):
            entry = _registry_entries.get(key)
            if entry is not None:
                return entry
        return None


def list_registered_texture_classifications() -> List[RegisteredTextureClassification]:
    with _registry_lock:
        _ensure_registry_loaded()
        return sorted(_registry_entries.values(), key=lambda entry: entry.path.casefold())


def set_registered_texture_classification(
    path_value: str,
    texture_type: str,
    semantic_subtype: str,
    *,
    source: str = "user",
    note: str = "",
) -> None:
    set_registered_texture_classifications(
        [path_value],
        texture_type,
        semantic_subtype,
        source=source,
        note=note,
    )


def set_registered_texture_classifications(
    path_values: Sequence[str],
    texture_type: str,
    semantic_subtype: str,
    *,
    source: str = "user",
    note: str = "",
) -> int:
    normalized_type = str(texture_type or "").strip().lower()
    normalized_subtype = str(semantic_subtype or "").strip().lower() or normalized_type
    if not normalized_type:
        raise ValueError("Texture type is required for a registered classification.")
    count = 0
    timestamp = datetime.now(timezone.utc).isoformat()
    with _registry_lock:
        _ensure_registry_loaded()
        for raw_path in path_values:
            normalized_path = str(raw_path or "").strip().replace("\\", "/").strip("/")
            if not normalized_path:
                continue
            key = normalize_texture_registry_key(normalized_path)
            _registry_entries[key] = RegisteredTextureClassification(
                path=normalized_path,
                texture_type=normalized_type,
                semantic_subtype=normalized_subtype,
                source=source,
                updated_at=timestamp,
                note=note.strip(),
            )
            count += 1
        if count:
            _write_registry_locked()
    return count


def remove_registered_texture_classifications(path_values: Sequence[str]) -> int:
    count = 0
    with _registry_lock:
        _ensure_registry_loaded()
        for raw_path in path_values:
            key = normalize_texture_registry_key(raw_path)
            if key in _registry_entries:
                _registry_entries.pop(key, None)
                count += 1
        if count:
            _write_registry_locked()
    return count


def _ensure_registry_loaded() -> None:
    global _registry_loaded, _registry_entries
    if _registry_loaded:
        return
    _registry_entries = {}
    if _registry_path is not None and _registry_path.exists():
        try:
            payload = json.loads(_registry_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        if isinstance(entries, list):
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    continue
                path_value = str(raw_entry.get("path", "")).strip().replace("\\", "/").strip("/")
                texture_type = str(raw_entry.get("texture_type", "")).strip().lower()
                semantic_subtype = str(raw_entry.get("semantic_subtype", "")).strip().lower() or texture_type
                if not path_value or not texture_type:
                    continue
                key = normalize_texture_registry_key(path_value)
                _registry_entries[key] = RegisteredTextureClassification(
                    path=path_value,
                    texture_type=texture_type,
                    semantic_subtype=semantic_subtype,
                    source=str(raw_entry.get("source", "user") or "user"),
                    updated_at=str(raw_entry.get("updated_at", "") or ""),
                    note=str(raw_entry.get("note", "") or ""),
                )
    _registry_loaded = True


def _write_registry_locked() -> None:
    if _registry_path is None:
        return
    _registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "entries": [asdict(entry) for entry in list_registered_texture_classifications()],
    }
    _registry_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
