from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from crimson_texture_forge.constants import *
from crimson_texture_forge.models import *
from crimson_texture_forge.core.common import *

def list_process_ids_by_image_name(image_name: str) -> set[int]:
    if os.name != "nt":
        return set()

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh", "/fi", f"IMAGENAME eq {image_name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )

    process_ids: set[int] = set()
    for line in split_log_lines(proc.stdout):
        if line.startswith("INFO:"):
            continue
        try:
            row = next(csv.reader([line]))
        except Exception:
            continue
        if len(row) < 2:
            continue
        name = row[0].strip().lower()
        if name != image_name.lower():
            continue
        try:
            process_ids.add(int(row[1].replace(",", "").strip()))
        except ValueError:
            continue

    return process_ids


def kill_process_tree_windows(pid: int) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )


def terminate_spawned_processes_by_image_name(
    image_name: str,
    preexisting_pids: set[int],
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[int, ...]:
    if os.name != "nt":
        return ()

    spawned_pids = tuple(sorted(list_process_ids_by_image_name(image_name) - preexisting_pids))
    if not spawned_pids:
        return ()

    if on_log:
        on_log(f"Stopping chaiNNer processes: {', '.join(str(pid) for pid in spawned_pids)}")

    for pid in reversed(spawned_pids):
        kill_process_tree_windows(pid)

    return spawned_pids


def wait_for_new_processes_to_finish(
    image_name: str,
    preexisting_pids: set[int],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if os.name != "nt":
        sleep_with_cancellation(CHAINNER_SETTLE_SECONDS, stop_event)
        return

    if on_log:
        on_log(f"Waiting for chaiNNer processes named {image_name} to finish...")

    last_reported: Optional[Tuple[int, ...]] = None
    quiet_since: Optional[float] = None

    while True:
        raise_if_cancelled(stop_event)
        current_pids = list_process_ids_by_image_name(image_name)
        spawned_pids = tuple(sorted(current_pids - preexisting_pids))

        if spawned_pids:
            quiet_since = None
            if spawned_pids != last_reported and on_log:
                on_log(f"chaiNNer still running. Active process IDs: {', '.join(str(pid) for pid in spawned_pids)}")
            last_reported = spawned_pids
            time.sleep(0.5)
            continue

        if quiet_since is None:
            quiet_since = time.monotonic()
            if on_log:
                on_log(f"chaiNNer processes have exited. Waiting {CHAINNER_SETTLE_SECONDS:.0f}s for file writes to settle...")
        elif time.monotonic() - quiet_since >= CHAINNER_SETTLE_SECONDS:
            break

        time.sleep(0.2)


def get_png_root_state(png_root: Path) -> Tuple[int, int]:
    if not png_root.exists() or not png_root.is_dir():
        return 0, 0

    count = 0
    latest_mtime_ns = 0
    for path in png_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        count += 1
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        latest_mtime_ns = max(latest_mtime_ns, mtime_ns)

    return count, latest_mtime_ns


def snapshot_png_outputs(png_root: Path) -> Dict[str, Tuple[int, int]]:
    snapshot: Dict[str, Tuple[int, int]] = {}
    if not png_root.exists() or not png_root.is_dir():
        return snapshot

    for path in png_root.rglob("*.png"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        relative_path = path.relative_to(png_root).as_posix().lower()
        snapshot[relative_path] = (stat.st_mtime_ns, stat.st_size)

    return snapshot


def summarize_png_output_changes(
    before_snapshot: Dict[str, Tuple[int, int]],
    after_snapshot: Dict[str, Tuple[int, int]],
) -> Tuple[int, Optional[str]]:
    changed_count = 0
    latest_relative_path: Optional[str] = None
    latest_mtime_ns = -1

    for relative_path, state in after_snapshot.items():
        if before_snapshot.get(relative_path) == state:
            continue
        changed_count += 1
        mtime_ns, _ = state
        if mtime_ns >= latest_mtime_ns:
            latest_mtime_ns = mtime_ns
            latest_relative_path = relative_path

    return changed_count, latest_relative_path


def substitute_chainner_tokens(value: object, token_map: Dict[str, str]) -> object:
    if isinstance(value, str):
        def replace_token(match: re.Match[str]) -> str:
            token_name = match.group(1)
            return token_map.get(token_name, match.group(0))

        return re.sub(r"\$\{([a-zA-Z0-9_]+)\}", replace_token, value)

    if isinstance(value, list):
        return [substitute_chainner_tokens(item, token_map) for item in value]

    if isinstance(value, dict):
        return {
            str(key): substitute_chainner_tokens(item, token_map)
            for key, item in value.items()
        }

    return value


def build_chainner_override_payload(config: NormalizedConfig) -> Optional[Dict[str, object]]:
    raw_json = config.chainner_override_json.strip()
    if not raw_json:
        return None

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chaiNNer override JSON is invalid: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("chaiNNer override JSON must be a JSON object.")

    if "inputs" in parsed:
        inputs = parsed.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("chaiNNer override JSON field 'inputs' must be a JSON object.")
        payload: Dict[str, object] = dict(parsed)
    else:
        payload = {"inputs": parsed}

    token_map = {
        "original_dds_root": str(config.original_dds_root),
        "png_root": str(config.png_root),
        "output_root": str(config.output_root),
        "texconv_path": str(config.texconv_path),
        "staging_png_root": str(config.dds_staging_root) if config.dds_staging_root is not None else "",
    }
    return substitute_chainner_tokens(payload, token_map)  # type: ignore[return-value]


def write_temp_json_file(payload: Dict[str, object]) -> Path:
    fd, temp_path = tempfile.mkstemp(prefix="crimson_texture_forge_chainner_", suffix=".json")
    os.close(fd)
    path = Path(temp_path)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def import_model_assets_to_directory(
    sources: Sequence[Path],
    destination_dir: Path,
    *,
    allowed_suffixes: Sequence[str],
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    destination_root = destination_dir.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    allowed = {suffix.lower() for suffix in allowed_suffixes}
    copied: List[Path] = []

    def copy_candidate(candidate: Path) -> None:
        if candidate.suffix.lower() not in allowed:
            return
        target = destination_root / candidate.name
        shutil.copy2(candidate, target)
        copied.append(target)
        if on_log:
            on_log(f"Imported model file: {target.name}")

    for source in sources:
        candidate = source.expanduser().resolve()
        if not candidate.exists():
            raise ValueError(f"Selected model source does not exist: {candidate}")
        if candidate.is_dir():
            for path in sorted(candidate.rglob("*")):
                if path.is_file():
                    copy_candidate(path)
            continue
        if candidate.is_file() and candidate.suffix.lower() == ".zip":
            with zipfile.ZipFile(candidate, "r") as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    member_path = Path(member.filename)
                    if member_path.suffix.lower() not in allowed:
                        continue
                    target = destination_root / member_path.name
                    with archive.open(member, "r") as source_handle, target.open("wb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle)
                    copied.append(target)
                    if on_log:
                        on_log(f"Imported model file: {target.name}")
            continue
        if candidate.is_file():
            copy_candidate(candidate)

    if not copied:
        raise ValueError(
            f"No supported model files were found. Expected one of: {', '.join(sorted(allowed))}"
        )
    return copied


def _iter_model_source_member_paths(sources: Sequence[Path]) -> List[Path]:
    discovered: List[Path] = []
    for source in sources:
        candidate = source.expanduser().resolve()
        if not candidate.exists():
            raise ValueError(f"Selected model source does not exist: {candidate}")
        if candidate.is_dir():
            for path in sorted(candidate.rglob("*")):
                if path.is_file():
                    discovered.append(path.relative_to(candidate))
            continue
        if candidate.is_file() and candidate.suffix.lower() == ".zip":
            with zipfile.ZipFile(candidate, "r") as archive:
                for member in archive.infolist():
                    if not member.is_dir():
                        discovered.append(Path(member.filename))
            continue
        if candidate.is_file():
            discovered.append(Path(candidate.name))
    return discovered


def validate_ncnn_model_import_sources(sources: Sequence[Path]) -> List[str]:
    member_paths = _iter_model_source_member_paths(sources)
    param_stems = {path.stem for path in member_paths if path.suffix.lower() == ".param"}
    bin_stems = {path.stem for path in member_paths if path.suffix.lower() == ".bin"}
    pairs = sorted(param_stems & bin_stems)
    if not pairs:
        raise ValueError(
            "No matching NCNN model pairs were found. Expected at least one .param + .bin pair "
            "with the same base name, for example 'realesr-animevideov3.param' and "
            "'realesr-animevideov3.bin'."
        )
    return pairs


def validate_onnx_model_import_sources(sources: Sequence[Path]) -> List[str]:
    member_paths = _iter_model_source_member_paths(sources)
    models = sorted({path.name for path in member_paths if path.suffix.lower() == ".onnx"})
    if not models:
        raise ValueError(
            "No .onnx files were found in the selected source. Choose a folder, zip, or file set "
            "that contains one or more ONNX model files."
        )
    return models


def resolve_python_package_install_interpreter() -> Optional[Path]:
    current_executable = Path(sys.executable).expanduser()
    if current_executable.name.lower().startswith("python") and current_executable.exists():
        return current_executable

    repo_root = Path(__file__).resolve().parents[2]
    venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return None


def install_python_packages(
    python_executable: Path,
    packages: Sequence[str],
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    if not python_executable.exists():
        raise ValueError(f"Python executable does not exist: {python_executable}")
    if not packages:
        raise ValueError("No Python packages were requested for installation.")

    cmd = [str(python_executable), "-m", "pip", "install", "--upgrade", *packages]
    if on_log:
        on_log(f"Running: {' '.join(cmd)}")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if on_log:
        for line in split_log_lines(proc.stdout):
            on_log(f"pip: {line}")
        for line in split_log_lines(proc.stderr):
            on_log(f"pip: {line}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"pip failed with exit code {proc.returncode}"
        raise ValueError(detail)
    return str(python_executable)


def _looks_like_windows_path(value: object) -> bool:
    return isinstance(value, str) and bool(re.match(r"^[A-Za-z]:[\\/]", value.strip()))


def _contains_supported_images(directory: Path) -> bool:
    if not directory.exists() or not directory.is_dir():
        return False
    try:
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_CHAINNER_LOAD_IMAGE_SUFFIXES:
                return True
    except OSError:
        return False
    return False


def _contains_any_files(directory: Path) -> bool:
    if not directory.exists() or not directory.is_dir():
        return False
    try:
        for path in directory.rglob("*"):
            if path.is_file():
                return True
    except OSError:
        return False
    return False


def _contains_dds_files(directory: Path) -> bool:
    if not directory.exists() or not directory.is_dir():
        return False
    try:
        for path in directory.rglob("*.dds"):
            if path.is_file():
                return True
    except OSError:
        return False
    return False


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def inspect_chainner_chain(chain_path: Path) -> ChainnerChainAnalysis:
    analysis = ChainnerChainAnalysis()
    try:
        payload = json.loads(chain_path.read_text(encoding="utf-8"))
    except Exception as exc:
        analysis.warnings.append(f"Could not inspect chaiNNer chain file: {exc}")
        return analysis

    nodes = payload.get("content", {}).get("nodes", [])
    if not isinstance(nodes, list):
        analysis.warnings.append("chaiNNer chain file has an unexpected structure; skipping path analysis.")
        return analysis

    analysis.node_count = len(nodes)
    for node in nodes:
        data = node.get("data", {})
        if not isinstance(data, dict):
            continue
        schema_id = str(data.get("schemaId", ""))
        if schema_id:
            analysis.schema_ids.append(schema_id)
        input_data = data.get("inputData", {})
        if not isinstance(input_data, dict):
            continue

        if schema_id == "chainner:image:load_images":
            raw = input_data.get("0")
            if _looks_like_windows_path(raw):
                analysis.load_image_dirs.append(Path(str(raw)).expanduser())
            analysis.load_image_globs.append(str(input_data.get("3", "**/*")))
            analysis.load_image_recursive.append(_boolish(input_data.get("2", False)))
        elif schema_id == "chainner:image:save":
            raw = input_data.get("1")
            if _looks_like_windows_path(raw):
                analysis.save_image_dirs.append(Path(str(raw)).expanduser())
            analysis.save_image_formats.append(str(input_data.get("4", "")).strip().lower())
        elif schema_id in {"chainner:ncnn:load_model", "chainner:pytorch:load_model"}:
            for raw in input_data.values():
                if _looks_like_windows_path(raw):
                    analysis.model_files.append(Path(str(raw)).expanduser())

        lowered_schema = schema_id.lower()
        if "upscale" in lowered_schema and schema_id not in analysis.upscaler_nodes:
            analysis.upscaler_nodes.append(schema_id)

    return analysis


def analyze_chainner_chain_paths(
    chain_path: Path,
    *,
    original_dds_root: Optional[Path],
    png_root: Optional[Path],
    chainner_override_json: str = "",
) -> ChainnerChainAnalysis:
    analysis = inspect_chainner_chain(chain_path)
    if analysis.warnings:
        return analysis

    chain_uses_overrides = bool(chainner_override_json.strip())
    if original_dds_root is not None and not _contains_any_files(original_dds_root):
        analysis.warnings.append(
            f"Original DDS root does not contain any files right now: {original_dds_root}"
        )

    if not analysis.upscaler_nodes:
        analysis.warnings.append(
            "No obvious chaiNNer upscale node was detected in the chain. "
            "Verify that the chain actually performs an upscale step."
        )

    if not chain_uses_overrides:
        if not analysis.load_image_dirs:
            analysis.warnings.append(
                "The chain does not expose any detectable Load Images folder path. "
                "If it uses dynamic inputs or custom nodes, verify the input path in chaiNNer directly."
            )
        for load_dir in analysis.load_image_dirs:
            if (
                original_dds_root is not None
                and png_root is not None
                and load_dir.resolve() != original_dds_root.resolve()
                and load_dir.resolve() != png_root.resolve()
            ):
                analysis.warnings.append(
                    f"The chain loads images from a hardcoded folder that does not match this app's roots: {load_dir}"
                )
            if not load_dir.exists():
                analysis.warnings.append(
                    f"The chaiNNer Load Images node points at a folder that does not exist: {load_dir}"
                )
            elif not _contains_any_files(load_dir):
                analysis.warnings.append(
                    f"The chaiNNer Load Images node points at {load_dir}, but that folder does not contain any files."
                )

        for save_dir in analysis.save_image_dirs:
            if png_root is not None and save_dir.resolve() != png_root.resolve():
                analysis.warnings.append(
                    f"The chain saves images to a hardcoded folder that does not match the configured PNG root: {save_dir}"
                )

        if not analysis.save_image_dirs:
            analysis.warnings.append(
                "No chaiNNer Save Images folder was detected. "
                "Verify that the chain writes image outputs the app can pick up."
            )

        non_png_formats = [fmt for fmt in analysis.save_image_formats if fmt and fmt != "png"]
        if non_png_formats:
            analysis.warnings.append(
                "The chain Save Images node is configured to write non-PNG output format(s): "
                + ", ".join(sorted(dict.fromkeys(non_png_formats)))
                + ". The DDS rebuild stage expects PNG files in PNG root."
            )

        missing_models = [model for model in analysis.model_files if not model.exists()]
        if missing_models:
            analysis.warnings.append(
                "The chain references model file(s) that do not exist: "
                + ", ".join(str(path) for path in missing_models)
            )

    return analysis


def analyze_chainner_chain(chain_path: Path, config: NormalizedConfig) -> ChainnerChainAnalysis:
    return analyze_chainner_chain_paths(
        chain_path,
        original_dds_root=config.original_dds_root,
        png_root=config.png_root,
        chainner_override_json=config.chainner_override_json,
    )


def format_chainner_analysis(analysis: ChainnerChainAnalysis, *, include_warnings: bool = True) -> str:
    lines: List[str] = []
    lines.append(f"Nodes: {analysis.node_count}")
    if analysis.upscaler_nodes:
        lines.append("Upscaler nodes:")
        for schema_id in analysis.upscaler_nodes:
            lines.append(f"- {schema_id}")
    else:
        lines.append("Upscaler nodes: none detected")

    if analysis.load_image_dirs:
        lines.append("")
        lines.append("Load Images:")
        for index, directory in enumerate(analysis.load_image_dirs):
            recursive = analysis.load_image_recursive[index] if index < len(analysis.load_image_recursive) else False
            glob_expr = analysis.load_image_globs[index] if index < len(analysis.load_image_globs) else "**/*"
            lines.append(f"- {directory}")
            lines.append(f"  recursive={ 'yes' if recursive else 'no' }, glob={glob_expr}")
    else:
        lines.append("")
        lines.append("Load Images: no load-images folders detected")

    if analysis.save_image_dirs:
        lines.append("")
        lines.append("Save Images:")
        for index, directory in enumerate(analysis.save_image_dirs):
            fmt = analysis.save_image_formats[index] if index < len(analysis.save_image_formats) else ""
            if fmt:
                lines.append(f"- {directory} (format={fmt})")
            else:
                lines.append(f"- {directory}")
    else:
        lines.append("")
        lines.append("Save Images: no save-images folders detected")

    if analysis.model_files:
        lines.append("")
        lines.append("Model files:")
        for model_path in analysis.model_files:
            suffix = "" if model_path.exists() else " [missing]"
            lines.append(f"- {model_path}{suffix}")

    if include_warnings:
        if analysis.warnings:
            lines.append("")
            lines.append("Validation warnings:")
            for warning in analysis.warnings:
                lines.append(f"- {warning}")
        else:
            lines.append("")
            lines.append("Validation: no obvious issues detected.")

    return "\n".join(lines)


def build_chainner_command(
    chainner_exe_path: Path,
    chainner_chain_path: Path,
    override_path: Optional[Path],
) -> List[str]:
    cmd = [str(chainner_exe_path), "run", str(chainner_chain_path)]
    if override_path is not None:
        cmd.extend(["--override", str(override_path)])
    return cmd


def build_chainner_env_overrides() -> Dict[str, Optional[str]]:
    overrides: Dict[str, Optional[str]] = {}
    for key in CHAINNER_ENV_VARS_TO_REMOVE:
        if os.environ.get(key):
            overrides[key] = None
    return overrides


def get_chainner_log_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "chaiNNer" / "logs" / "main.log"


def normalize_chainner_log_line(raw_line: str) -> str:
    text = raw_line.strip()
    match = re.match(r"^\[[^\]]+\]\s+\[[^\]]+\]\s+(.*)$", text)
    if match:
        return match.group(1).strip()
    return text


def normalize_chainner_console_line(raw_line: str) -> str:
    text = normalize_chainner_log_line(raw_line)
    text = re.sub(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*>\s*", "", text)
    return text.strip()


def should_suppress_chainner_noise_message(message: str) -> bool:
    lowered = message.lower().strip()
    if not lowered:
        return True
    return (
        "body not consumed" in lowered
        or "log.catcherrors is deprecated" in lowered
        or ("request: get /sse" in lowered and "body not consumed" in lowered)
    )


def should_emit_chainner_log_line(message: str) -> bool:
    if should_suppress_chainner_noise_message(message):
        return False
    lowered = message.lower()
    return (
        lowered.startswith("read chain file")
        or "executed " in lowered and " nodes" in lowered
        or lowered == "done."
        or lowered.startswith("cleaning up temp folders")
        or "[warning]" in lowered
        or "[error]" in lowered
        or lowered.startswith("error")
        or "unable to upscale" in lowered
    )


class ChainnerLogMonitor:
    def __init__(
        self,
        log_path: Optional[Path],
        *,
        on_log: Optional[Callable[[str], None]] = None,
        on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        self.log_path = log_path
        self.on_log = on_log
        self.on_phase_progress = on_phase_progress
        self.offset = 0
        self.partial_line = ""
        self.last_progress: Optional[Tuple[int, int]] = None
        self.last_emitted_message: Optional[str] = None
        self.seen_messages: set[str] = set()

        if self.log_path and self.log_path.exists():
            try:
                self.offset = self.log_path.stat().st_size
            except OSError:
                self.offset = 0

    def poll(self) -> None:
        if self.log_path is None:
            return

        try:
            if not self.log_path.exists():
                return
            size = self.log_path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.partial_line = ""
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return

        if not chunk:
            return

        text = self.partial_line + chunk
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if normalized and not normalized.endswith("\n"):
            self.partial_line = lines.pop()
        else:
            self.partial_line = ""

        for line in lines:
            self._handle_line(line)

    def flush(self) -> None:
        self.poll()
        if self.partial_line.strip():
            self._handle_line(self.partial_line)
            self.partial_line = ""

    def _handle_line(self, raw_line: str) -> None:
        message = normalize_chainner_log_line(raw_line)
        if not message:
            return
        if should_suppress_chainner_noise_message(message):
            return

        progress_match = CHAINNER_PROGRESS_RE.search(message)
        if progress_match and self.on_phase_progress:
            current = int(progress_match.group(1))
            total = int(progress_match.group(2))
            progress = (current, total)
            if progress != self.last_progress:
                self.last_progress = progress
                self.on_phase_progress(current, total, f"{current} / {total} nodes")
        elif message.lower() == "done." and self.on_phase_progress and self.last_progress is not None:
            current, total = self.last_progress
            if current < total:
                self.last_progress = (total, total)
                self.on_phase_progress(total, total, f"{total} / {total} nodes")

        if should_emit_chainner_log_line(message) and message != self.last_emitted_message:
            self.last_emitted_message = message
            self.seen_messages.add(message)
            if self.on_log:
                self.on_log(f"chaiNNer: {message}")


class ChainnerPngProgressMonitor:
    def __init__(
        self,
        png_root: Path,
        before_snapshot: Dict[str, Tuple[int, int]],
        expected_total: int,
        *,
        on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
        on_current_file: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.png_root = png_root
        self.before_snapshot = before_snapshot
        self.expected_total = max(0, expected_total)
        self.on_phase_progress = on_phase_progress
        self.on_current_file = on_current_file
        self.last_reported_count = -1
        self.last_reported_file: Optional[str] = None
        self.last_scan_time = 0.0

    def poll(self, force: bool = False) -> None:
        if self.expected_total <= 0:
            return

        now = time.monotonic()
        if not force and now - self.last_scan_time < 0.75:
            return
        self.last_scan_time = now

        after_snapshot = snapshot_png_outputs(self.png_root)
        changed_count, latest_relative_path = summarize_png_output_changes(self.before_snapshot, after_snapshot)
        capped_count = min(changed_count, self.expected_total)

        if capped_count != self.last_reported_count and self.on_phase_progress:
            self.last_reported_count = capped_count
            self.on_phase_progress(capped_count, self.expected_total, f"{capped_count} / {self.expected_total} PNG outputs")

        if latest_relative_path and latest_relative_path != self.last_reported_file and self.on_current_file:
            self.last_reported_file = latest_relative_path
            self.on_current_file(f"Upscaled: {latest_relative_path}")

    def flush(self) -> None:
        self.poll(force=True)


def detect_chainner_launch_failure(stdout: str, stderr: str) -> Optional[str]:
    combined = "\n".join(part for part in (stdout, stderr) if part).lower()
    if "cannot find module" in combined or "module_not_found" in combined:
        return (
            "chaiNNer did not start correctly. It appears to have launched in Node mode instead of "
            "its normal Electron/CLI mode."
        )
    return None


def detect_chainner_runtime_failure(stdout: str, stderr: str) -> Optional[str]:
    combined = "\n".join(part for part in (stdout, stderr) if part)
    match = CHAINNER_NO_VALID_IMAGES_RE.search(combined)
    if match:
        directory = match.group("directory").strip()
        return (
            f"chaiNNer could not find valid images in {directory}. "
            "That usually means the folder is empty, the chain points at the wrong path, or the Load Images node "
            "for this chain is not configured to read the files in that directory. "
            "If your chain is meant to read DDS directly, verify that in chaiNNer. Otherwise enable "
            "'Convert DDS to PNG before processing' and point the chain or overrides at ${staging_png_root} "
            "or another PNG folder."
        )
    return None


def run_chainner_stage(
    config: NormalizedConfig,
    *,
    expected_output_total: int = 0,
    on_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if not config.enable_chainner:
        return

    if on_phase:
        on_phase("Upscaling", "Running chaiNNer chain...", True)
    if on_current_file:
        on_current_file(config.chainner_chain_path.name if config.chainner_chain_path else "chaiNNer chain")
    if on_log:
        on_log("Phase 1/2: running chaiNNer.")
    if on_phase_progress:
        if expected_output_total > 0:
            on_phase_progress(0, expected_output_total, f"0 / {expected_output_total} PNG outputs")
        else:
            on_phase_progress(0, 0, "Starting chaiNNer...")

    if config.dry_run:
        if on_log:
            on_log("Dry run is enabled, so the chaiNNer stage is skipped.")
        return

    if config.chainner_exe_path is None or config.chainner_chain_path is None:
        raise ValueError("chaiNNer is enabled, but the chaiNNer executable or chain path is missing.")

    chain_analysis = analyze_chainner_chain(config.chainner_chain_path, config)
    if on_log:
        for warning in chain_analysis.warnings:
            on_log(f"chaiNNer preflight: {warning}")

    config.png_root.mkdir(parents=True, exist_ok=True)
    before_png_state = get_png_root_state(config.png_root)
    before_png_snapshot = snapshot_png_outputs(config.png_root)
    process_name = config.chainner_exe_path.name
    preexisting_pids = list_process_ids_by_image_name(process_name)
    chainner_log_monitor = ChainnerLogMonitor(
        get_chainner_log_path(),
        on_log=on_log,
        on_phase_progress=on_phase_progress if expected_output_total <= 0 else None,
    )
    png_progress_monitor = ChainnerPngProgressMonitor(
        config.png_root,
        before_png_snapshot,
        expected_output_total,
        on_phase_progress=on_phase_progress,
        on_current_file=on_current_file,
    )

    override_path: Optional[Path] = None
    try:
        override_payload = build_chainner_override_payload(config)
        if override_payload is not None:
            override_path = write_temp_json_file(override_payload)
            if on_log:
                on_log(f"Generated temporary chaiNNer override file: {override_path}")

        cmd = build_chainner_command(config.chainner_exe_path, config.chainner_chain_path, override_path)
        env_overrides = build_chainner_env_overrides()
        if on_log:
            on_log(f"Launching chaiNNer chain: {config.chainner_chain_path}")
            if env_overrides:
                removed_vars = ", ".join(sorted(env_overrides))
                on_log(f"Sanitizing chaiNNer environment variables: {removed_vars}")
            if expected_output_total > 0:
                on_log(
                    "Tracking chaiNNer progress by counting PNG files created or updated in the configured PNG root."
                )

        def poll_chainner_monitors() -> None:
            chainner_log_monitor.poll()
            png_progress_monitor.poll()

        return_code, stdout, stderr = run_process_with_cancellation(
            cmd,
            stop_event=stop_event,
            env_overrides=env_overrides or None,
            on_poll=poll_chainner_monitors,
            on_cancel=lambda _proc: terminate_spawned_processes_by_image_name(
                process_name,
                preexisting_pids,
                on_log=on_log,
            ),
        )
        chainner_log_monitor.flush()
        png_progress_monitor.flush()
        for line in split_log_lines(stdout):
            message = normalize_chainner_console_line(line)
            if not message or should_suppress_chainner_noise_message(message) or message in chainner_log_monitor.seen_messages:
                continue
            if on_log:
                on_log(f"chaiNNer: {message}")
        for line in split_log_lines(stderr):
            message = normalize_chainner_console_line(line)
            if not message or should_suppress_chainner_noise_message(message) or message in chainner_log_monitor.seen_messages:
                continue
            if on_log:
                on_log(f"chaiNNer: {message}")

        launch_failure = detect_chainner_launch_failure(stdout, stderr)
        if launch_failure is not None:
            raise ValueError(launch_failure)

        runtime_failure = detect_chainner_runtime_failure(stdout, stderr)
        if runtime_failure is not None:
            raise ValueError(runtime_failure)

        if return_code != 0:
            raise ValueError(
                f"chaiNNer failed with exit code {return_code}. "
                "The chaiNNer CLI is documented as experimental, so confirm the chain also works in the GUI."
            )

        wait_for_new_processes_to_finish(
            process_name,
            preexisting_pids,
            on_log=on_log,
            stop_event=stop_event,
        )

        after_png_state = get_png_root_state(config.png_root)
        png_progress_monitor.flush()
        if on_phase_progress and expected_output_total <= 0 and chainner_log_monitor.last_progress is not None:
            _, total_nodes = chainner_log_monitor.last_progress
            on_phase_progress(total_nodes, total_nodes, f"{total_nodes} / {total_nodes} nodes")
        if on_log:
            on_log("chaiNNer completed successfully.")
            if after_png_state == before_png_state:
                message = (
                    "Warning: chaiNNer exited successfully, but the PNG root did not appear to change. "
                    "If this chain writes elsewhere or needs unsupported override types, verify the chain in chaiNNer directly."
                )
                if chain_analysis.warnings:
                    message += " Preflight issues: " + " | ".join(chain_analysis.warnings[:3])
                on_log(message)
    except RunCancelled:
        terminate_spawned_processes_by_image_name(
            process_name,
            preexisting_pids,
            on_log=on_log,
        )
        chainner_log_monitor.flush()
        png_progress_monitor.flush()
        raise
    finally:
        if override_path is not None:
            try:
                override_path.unlink(missing_ok=True)
            except OSError:
                pass

