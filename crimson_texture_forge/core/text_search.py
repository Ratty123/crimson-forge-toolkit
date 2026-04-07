from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from crimson_texture_forge.constants import ARCHIVE_TEXT_EXTENSIONS, ARCHIVE_TEXT_PREVIEW_LIMIT
from crimson_texture_forge.core.archive import (
    ArchiveEntry,
    extract_archive_entry,
    find_available_output_path,
    parse_archive_note_flags,
    read_archive_entry_data,
    sanitize_archive_entry_output_path,
)
from crimson_texture_forge.core.common import raise_if_cancelled
from crimson_texture_forge.models import RunCancelled

DEFAULT_TEXT_SEARCH_EXTENSIONS = ".xml;.txt;.json;.cfg;.ini;.lua;.material;.shader;.yaml;.yml"
TEXT_SEARCH_PREVIEW_LIMIT = max(ARCHIVE_TEXT_PREVIEW_LIMIT, 12_000_000)
TEXT_SEARCH_HIGHLIGHT_LIMIT = 2_000


@dataclass(slots=True)
class TextSearchResult:
    source_kind: str
    relative_path: str
    extension: str
    match_count: int
    snippet: str
    package_label: str = ""
    archive_entry: Optional[ArchiveEntry] = None
    loose_root: Optional[Path] = None
    loose_path: Optional[Path] = None


@dataclass(slots=True)
class TextSearchPreview:
    title: str
    metadata: str
    detail_text: str
    preview_text: str
    match_spans: List[Tuple[int, int]]
    truncated: bool = False


@dataclass(slots=True)
class TextSearchRunStats:
    source_kind: str
    candidate_count: int
    searched_count: int
    decrypted_count: int = 0
    skipped_read_error_count: int = 0


def normalize_text_search_extensions(raw_value: str) -> Tuple[str, ...]:
    raw = raw_value.strip()
    if not raw:
        raw = DEFAULT_TEXT_SEARCH_EXTENSIONS

    extensions: List[str] = []
    seen: set[str] = set()
    for token in re.split(r"[,\s;|]+", raw):
        token = token.strip()
        if not token:
            continue
        if token == "*":
            for ext in sorted(ARCHIVE_TEXT_EXTENSIONS):
                if ext not in seen:
                    seen.add(ext)
                    extensions.append(ext)
            continue
        if not token.startswith("."):
            token = f".{token}"
        normalized = token.lower()
        if normalized not in seen:
            seen.add(normalized)
            extensions.append(normalized)
    return tuple(extensions)


def _compile_search_pattern(query: str, *, regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    if not query.strip():
        raise ValueError("Enter a search string or regular expression.")
    pattern_text = query if regex else re.escape(query)
    flags = re.MULTILINE
    if not case_sensitive:
        flags |= re.IGNORECASE
    try:
        return re.compile(pattern_text, flags)
    except re.error as exc:
        raise ValueError(f"Invalid search pattern: {exc}") from exc


def _find_match_spans(text: str, pattern: re.Pattern[str], *, limit: Optional[int] = None) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for match in pattern.finditer(text):
        start, end = match.span()
        if end <= start:
            continue
        spans.append((start, end))
        if limit is not None and len(spans) >= limit:
            break
    return spans


def _path_matches_filter(path_value: str, path_filter: str) -> bool:
    if not path_filter:
        return True
    return path_filter.lower() in path_value.lower()


def _build_match_snippet(text: str, spans: Sequence[Tuple[int, int]], *, radius: int = 100) -> str:
    if not spans:
        preview = text.strip().splitlines()
        return preview[0][: radius * 2] if preview else ""
    start, end = spans[0]
    snippet_start = max(0, start - radius)
    snippet_end = min(len(text), end + radius)
    snippet = text[snippet_start:snippet_end].replace("\r", "")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    prefix = "..." if snippet_start > 0 else ""
    suffix = "..." if snippet_end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _decode_text_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _iter_loose_text_files(root: Path, extension_filters: Sequence[str], path_filter: str) -> List[Path]:
    files: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extension_filters:
            continue
        relative_path = path.relative_to(root).as_posix()
        if not _path_matches_filter(relative_path, path_filter):
            continue
        files.append(path)
    return files


def search_archive_text_entries(
    entries: Sequence[ArchiveEntry],
    query: str,
    *,
    extension_filters: Sequence[str],
    path_filter: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[List[TextSearchResult], TextSearchRunStats]:
    pattern = _compile_search_pattern(query, regex=regex, case_sensitive=case_sensitive)
    candidates = [
        entry
        for entry in entries
        if entry.extension in extension_filters and _path_matches_filter(entry.path, path_filter)
    ]
    results: List[TextSearchResult] = []
    searched_count = 0
    decrypted_count = 0
    skipped_read_error_count = 0
    total = len(candidates)
    encrypted_candidate_count = sum(1 for entry in candidates if entry.encrypted)
    if on_log:
        on_log(f"Searching {total:,} archive text-like file(s).")
        if encrypted_candidate_count:
            on_log(
                f"Found {encrypted_candidate_count:,} encrypted candidate file(s). They will be decrypted when supported."
            )
    for index, entry in enumerate(candidates, start=1):
        raise_if_cancelled(stop_event, "Text search stopped by user.")
        if on_progress:
            on_progress(index - 1, total, f"Searching archive text files... {entry.path}")
        try:
            data, _decompressed, note = read_archive_entry_data(entry)
        except Exception as exc:
            skipped_read_error_count += 1
            if on_log:
                on_log(f"Warning: could not read {entry.path}: {exc}")
            continue
        searched_count += 1
        note_flags = parse_archive_note_flags(note)
        if "ChaCha20" in note_flags:
            decrypted_count += 1
        text = _decode_text_bytes(data)
        spans = _find_match_spans(text, pattern, limit=TEXT_SEARCH_HIGHLIGHT_LIMIT + 1)
        if not spans:
            continue
        match_count = len(spans)
        if match_count > TEXT_SEARCH_HIGHLIGHT_LIMIT:
            match_count = sum(1 for match in pattern.finditer(text) if match.end() > match.start())
            spans = spans[:TEXT_SEARCH_HIGHLIGHT_LIMIT]
        snippet = _build_match_snippet(text, spans)
        if note and on_log:
            on_log(f"Matched {entry.path} [{note}]")
        results.append(
            TextSearchResult(
                source_kind="archive",
                relative_path=entry.path.replace("\\", "/"),
                extension=entry.extension,
                match_count=match_count,
                snippet=snippet,
                package_label=entry.package_label,
                archive_entry=entry,
            )
        )
    if on_progress:
        on_progress(total, total, f"Archive text search complete. Found {len(results):,} matching file(s).")
    return results, TextSearchRunStats(
        source_kind="archive",
        candidate_count=total,
        searched_count=searched_count,
        decrypted_count=decrypted_count,
        skipped_read_error_count=skipped_read_error_count,
    )


def search_loose_text_files(
    root: Path,
    query: str,
    *,
    extension_filters: Sequence[str],
    path_filter: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[List[TextSearchResult], TextSearchRunStats]:
    pattern = _compile_search_pattern(query, regex=regex, case_sensitive=case_sensitive)
    candidates = _iter_loose_text_files(root, extension_filters, path_filter)
    results: List[TextSearchResult] = []
    skipped_read_error_count = 0
    total = len(candidates)
    if on_log:
        on_log(f"Searching {total:,} loose text-like file(s) under {root}.")
    for index, path in enumerate(candidates, start=1):
        raise_if_cancelled(stop_event, "Text search stopped by user.")
        relative_path = path.relative_to(root).as_posix()
        if on_progress:
            on_progress(index - 1, total, f"Searching loose text files... {relative_path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            skipped_read_error_count += 1
            if on_log:
                on_log(f"Warning: could not read {path}: {exc}")
            continue
        text = _decode_text_bytes(data)
        spans = _find_match_spans(text, pattern, limit=TEXT_SEARCH_HIGHLIGHT_LIMIT + 1)
        if not spans:
            continue
        match_count = len(spans)
        if match_count > TEXT_SEARCH_HIGHLIGHT_LIMIT:
            match_count = sum(1 for match in pattern.finditer(text) if match.end() > match.start())
            spans = spans[:TEXT_SEARCH_HIGHLIGHT_LIMIT]
        results.append(
            TextSearchResult(
                source_kind="loose",
                relative_path=relative_path,
                extension=path.suffix.lower(),
                match_count=match_count,
                snippet=_build_match_snippet(text, spans),
                loose_root=root,
                loose_path=path,
            )
        )
    if on_progress:
        on_progress(total, total, f"Loose text search complete. Found {len(results):,} matching file(s).")
    return results, TextSearchRunStats(
        source_kind="loose",
        candidate_count=total,
        searched_count=total - skipped_read_error_count,
        skipped_read_error_count=skipped_read_error_count,
    )


def _build_preview_window(
    text: str,
    spans: Sequence[Tuple[int, int]],
    *,
    max_chars: int = TEXT_SEARCH_PREVIEW_LIMIT,
) -> Tuple[str, List[Tuple[int, int]], bool]:
    if max_chars > 0 and len(text) > max_chars:
        preview_text = text[:max_chars]
        preview_spans = []
        for start, end in spans[:TEXT_SEARCH_HIGHLIGHT_LIMIT]:
            if start >= max_chars:
                break
            preview_spans.append((start, min(end, max_chars)))
        return preview_text, preview_spans, True
    return text, list(spans[:TEXT_SEARCH_HIGHLIGHT_LIMIT]), False


def load_text_search_preview(
    result: TextSearchResult,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
) -> TextSearchPreview:
    pattern = _compile_search_pattern(query, regex=regex, case_sensitive=case_sensitive)
    if result.source_kind == "archive":
        if result.archive_entry is None:
            raise ValueError("Archive search result is missing its archive entry reference.")
        data, _decompressed, note = read_archive_entry_data(result.archive_entry)
        text = _decode_text_bytes(data)
        detail_lines = [
            f"Path: {result.relative_path}",
            f"Package: {result.package_label}",
        ]
        note_flags = parse_archive_note_flags(note)
        if note:
            detail_lines.append(f"Read note: {note}")
        if "ChaCha20" in note_flags:
            detail_lines.append("Archive XML decrypted via deterministic ChaCha20 filename derivation.")
        title = result.relative_path
        metadata = f"{result.extension or 'no extension'} | {result.match_count:,} match(es) | {result.package_label}"
    else:
        if result.loose_path is None or result.loose_root is None:
            raise ValueError("Loose search result is missing its file path reference.")
        data = result.loose_path.read_bytes()
        text = _decode_text_bytes(data)
        title = result.relative_path
        metadata = f"{result.extension or 'no extension'} | {result.match_count:,} match(es) | Loose file"
        detail_lines = [
            f"Loose root: {result.loose_root}",
            f"Path: {result.loose_path}",
        ]

    spans = _find_match_spans(text, pattern, limit=TEXT_SEARCH_HIGHLIGHT_LIMIT)
    preview_text, preview_spans, truncated = _build_preview_window(text, spans)
    if result.match_count > len(preview_spans):
        detail_lines.append(
            f"Preview highlights the first {len(preview_spans):,} match(es) to keep the viewer responsive."
        )
    if truncated:
        detail_lines.append(
            f"Preview truncated to the first {format(len(preview_text), ',')} character(s) to keep the viewer responsive."
        )

    return TextSearchPreview(
        title=title,
        metadata=metadata,
        detail_text="\n".join(detail_lines),
        preview_text=preview_text,
        match_spans=preview_spans,
        truncated=truncated,
    )


def sanitize_loose_export_path(relative_path: str, output_root: Path) -> Path:
    pure_path = PurePosixPath(relative_path.replace("\\", "/"))
    safe_parts = [part for part in pure_path.parts if part not in {"", ".", ".."}]
    if not safe_parts:
        raise ValueError(f"Loose file has an invalid path: {relative_path}")
    return output_root.joinpath(*safe_parts)


def export_text_search_results(
    results: Sequence[TextSearchResult],
    output_root: Path,
    *,
    collision_mode: str = "overwrite",
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, int]:
    output_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    renamed = 0
    failed = 0
    reserved_paths: set[str] = set()

    for index, result in enumerate(results, start=1):
        raise_if_cancelled(stop_event, "Text export stopped by user.")
        try:
            if result.source_kind == "archive":
                if result.archive_entry is None:
                    raise ValueError("Missing archive entry for export.")
                target_path = sanitize_archive_entry_output_path(result.archive_entry, output_root)
            else:
                if result.loose_path is None:
                    raise ValueError("Missing loose file path for export.")
                target_path = sanitize_loose_export_path(result.relative_path, output_root)

            if collision_mode == "rename":
                resolved_path = find_available_output_path(target_path, reserved_paths)
                if resolved_path != target_path:
                    renamed += 1
            else:
                resolved_path = target_path

            reserved_paths.add(str(resolved_path).lower())
            if result.source_kind == "archive":
                extract_archive_entry(result.archive_entry, resolved_path)  # type: ignore[arg-type]
            else:
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result.loose_path, resolved_path)  # type: ignore[arg-type]
            extracted += 1
            if on_log:
                extra = " [Renamed]" if resolved_path != target_path else ""
                on_log(f"[{index}/{len(results)}] EXPORT {result.relative_path}{extra} -> {resolved_path}")
        except RunCancelled:
            raise
        except Exception as exc:
            failed += 1
            if on_log:
                on_log(f"[{index}/{len(results)}] FAIL {result.relative_path} -> {exc}")

    return {
        "total": len(results),
        "exported": extracted,
        "renamed": renamed,
        "failed": failed,
    }
