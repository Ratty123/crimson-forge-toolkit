from __future__ import annotations

import shlex
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

try:
    from PIL import Image as PilImage
except Exception:  # pragma: no cover - optional validation helper
    PilImage = None  # type: ignore[assignment]

from cdmw.constants import (
    UPSCALE_BACKEND_REALESRGAN_NCNN,
    UPSCALE_POST_CORRECTION_NONE,
    UPSCALE_TEXTURE_PRESET_ALL,
    UPSCALE_TEXTURE_PRESET_BALANCED,
    UPSCALE_TEXTURE_PRESET_COLOR_UI,
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
)
from cdmw.core.common import raise_if_cancelled, run_process_with_cancellation
from cdmw.core.upscale_postprocess import (
    apply_post_upscale_color_correction,
    build_source_match_plan_for_decision,
    describe_post_upscale_correction_mode,
)
from cdmw.core.upscale_profiles import (
    build_ncnn_retry_tile_candidates,
    copy_mod_ready_loose_tree,
    describe_texture_preset,
)
from cdmw.models import NormalizedConfig, TextureProcessingPlan

_NCNN_RUNTIME_FAILURE_MARKERS = (
    "vkallocatememory failed",
    "failed to allocate",
    "out of memory",
    "vk_error_out_of_device_memory",
    "vk_error_out_of_host_memory",
)


def resolve_ncnn_model_dir(ncnn_exe_path: Optional[Path], explicit_model_dir: Optional[Path]) -> Optional[Path]:
    if explicit_model_dir is not None:
        return explicit_model_dir
    if ncnn_exe_path is None:
        return None
    default_dir = ncnn_exe_path.parent / "models"
    if default_dir.exists() and default_dir.is_dir():
        return default_dir
    return None


def discover_realesrgan_ncnn_models(
    ncnn_exe_path: Optional[Path],
    model_dir: Optional[Path],
) -> List[Tuple[str, Path]]:
    resolved_dir = resolve_ncnn_model_dir(ncnn_exe_path, model_dir)
    if resolved_dir is None or not resolved_dir.exists() or not resolved_dir.is_dir():
        return []

    discovered: List[Tuple[str, Path]] = []
    for param_path in sorted(resolved_dir.glob("*.param")):
        if not param_path.is_file():
            continue
        bin_path = param_path.with_suffix(".bin")
        if not bin_path.exists():
            continue
        discovered.append((param_path.stem, resolved_dir))
    return discovered


def build_realesrgan_ncnn_command(
    ncnn_exe_path: Path,
    *,
    input_path: Path,
    output_path: Path,
    model_dir: Path,
    model_name: str,
    scale: int,
    tile_size: int,
    extra_args: Sequence[str] = (),
) -> List[str]:
    cmd = [
        str(ncnn_exe_path),
        "-i",
        str(input_path),
        "-o",
        str(output_path),
        "-m",
        str(model_dir),
        "-n",
        model_name,
        "-s",
        str(scale),
        "-t",
        str(tile_size),
        "-f",
        "png",
    ]
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args if str(arg).strip())
    return cmd


def parse_realesrgan_ncnn_extra_args(raw_text: str) -> List[str]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError(f"Real-ESRGAN NCNN extra args are invalid: {exc}") from exc


def _detect_ncnn_runtime_failure(stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".strip().lower()
    for marker in _NCNN_RUNTIME_FAILURE_MARKERS:
        if marker in combined:
            return marker
    return ""


def _png_channel_spans(png_path: Path) -> Optional[Tuple[int, int, int]]:
    if PilImage is None:
        return None
    with PilImage.open(png_path) as image:
        rgba = image.convert("RGBA")
        extrema = rgba.getextrema()
    return tuple(int(channel_max) - int(channel_min) for channel_min, channel_max in extrema[:3])


def _detect_corrupt_ncnn_output(input_png: Path, output_png: Path) -> str:
    if not output_png.exists() or not output_png.is_file():
        return f"Real-ESRGAN NCNN did not produce an output PNG: {output_png.name}"
    try:
        if output_png.stat().st_size <= 0:
            return f"Real-ESRGAN NCNN produced an empty output PNG: {output_png.name}"
    except OSError:
        return f"Could not inspect Real-ESRGAN NCNN output PNG: {output_png.name}"
    if PilImage is None:
        return ""
    try:
        input_spans = _png_channel_spans(input_png)
        output_spans = _png_channel_spans(output_png)
    except Exception as exc:
        return f"Could not validate Real-ESRGAN NCNN output PNG '{output_png.name}': {exc}"
    if input_spans is None or output_spans is None:
        return ""
    if max(input_spans) >= 8 and max(output_spans) <= 1:
        return (
            f"Real-ESRGAN NCNN produced a nearly flat output PNG for {output_png.name} "
            f"(input spans={input_spans}, output spans={output_spans})."
        )
    return ""


def _run_single_ncnn_attempt(
    config: NormalizedConfig,
    *,
    input_root: Path,
    output_root: Path,
    processing_plan: Sequence[TextureProcessingPlan] = (),
    on_log: Optional[Callable[[str], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    plan_entries = [entry for entry in processing_plan if entry.action == "upscale_then_rebuild"]
    if not plan_entries:
        if on_log:
            on_log("No PNG files require Real-ESRGAN NCNN processing under the current plan; skipping backend stage.")
        if on_phase_progress:
            on_phase_progress(0, 0, "0 / 0 PNG files")
        return

    png_inputs = [input_root / entry.relative_path.with_suffix(".png") for entry in plan_entries]
    total = len(png_inputs)
    if total == 0:
        raise ValueError(
            f"No PNG files were found for Real-ESRGAN NCNN in {input_root}. "
            "Enable DDS staging first or populate PNG root with source PNG files."
        )

    if on_log:
        on_log(f"Real-ESRGAN NCNN executable: {config.ncnn_exe_path}")
        on_log(f"Real-ESRGAN NCNN model folder: {config.ncnn_model_dir}")
        unique_models = sorted({entry.effective_ncnn_settings.model_name for entry in plan_entries if entry.effective_ncnn_settings.model_name})
        unique_scales = sorted({int(entry.effective_ncnn_settings.scale) for entry in plan_entries if entry.effective_ncnn_settings.scale})
        unique_tiles = sorted({int(entry.effective_ncnn_settings.tile_size) for entry in plan_entries})
        if unique_models:
            if len(unique_models) == 1:
                on_log(f"Real-ESRGAN NCNN model: {unique_models[0]}")
            else:
                on_log(f"Real-ESRGAN NCNN models in plan: {', '.join(unique_models)}")
        if unique_scales:
            on_log(f"Real-ESRGAN NCNN plan scales: {', '.join(f'{scale}x' for scale in unique_scales)}")
        if unique_tiles:
            on_log(f"Real-ESRGAN NCNN plan tile sizes: {', '.join(str(tile) for tile in unique_tiles)}")
        on_log(describe_texture_preset(config.upscale_texture_preset))

    if on_phase_progress:
        on_phase_progress(0, total, f"0 / {total} PNG files")

    assert config.ncnn_exe_path is not None
    assert config.ncnn_model_dir is not None
    parsed_extra_args_cache: dict[str, List[str]] = {}

    for index, plan_entry in enumerate(plan_entries, start=1):
        raise_if_cancelled(stop_event)
        input_png = input_root / plan_entry.relative_path.with_suffix(".png")
        if not input_png.exists() or not input_png.is_file():
            raise ValueError(f"Expected planner-selected PNG does not exist: {input_png}")
        rel_path = input_png.relative_to(input_root)
        rel_display = rel_path.as_posix()
        decision = plan_entry.decision
        effective_settings = plan_entry.effective_ncnn_settings
        correction_mode = effective_settings.post_correction_mode or config.upscale_post_correction_mode
        correction_plan = build_source_match_plan_for_decision(
            correction_mode,
            decision,
            direct_backend_supported=True,
            planner_path_kind=plan_entry.path_kind,
            planner_profile_key=plan_entry.profile.key,
        )
        texture_type = decision.texture_type or "unknown"
        output_png = output_root / rel_path
        output_png.parent.mkdir(parents=True, exist_ok=True)
        if on_current_file:
            on_current_file(f"Upscale: {rel_display}")

        extra_args_text = effective_settings.extra_args or ""
        if extra_args_text not in parsed_extra_args_cache:
            parsed_extra_args_cache[extra_args_text] = parse_realesrgan_ncnn_extra_args(extra_args_text)
        parsed_extra_args = parsed_extra_args_cache[extra_args_text]

        retry_plan = build_ncnn_retry_tile_candidates(effective_settings.tile_size, include_full_frame_fallback=False)
        attempt_tiles = (retry_plan.requested_tile_size, *retry_plan.candidate_tile_sizes)

        action = "DRYRUN" if config.dry_run else "UPSCALE"
        if on_log:
            on_log(
                f"[{index}/{total}] {action} {rel_display} [{texture_type}] "
                f"model={effective_settings.model_name} scale={effective_settings.scale}x "
                f"tile={effective_settings.tile_size}"
            )
            if extra_args_text:
                on_log(f"[{index}/{total}] NCNN extra args for {rel_display}: {extra_args_text}")
            on_log(
                f"[{index}/{total}] NCNN post correction for {rel_display}: "
                f"{describe_post_upscale_correction_mode(correction_mode)}"
            )

        if not config.dry_run:
            last_detail = ""
            succeeded = False
            for attempt_index, tile_size in enumerate(attempt_tiles, start=1):
                raise_if_cancelled(stop_event)
                if on_log and len(attempt_tiles) > 1:
                    on_log(
                        f"[{index}/{total}] NCNN tile attempt {attempt_index}/{len(attempt_tiles)} "
                        f"for {rel_display}: tile={tile_size}"
                    )
                try:
                    if output_png.exists():
                        output_png.unlink()
                except OSError:
                    pass
                cmd = build_realesrgan_ncnn_command(
                    config.ncnn_exe_path,
                    input_path=input_png,
                    output_path=output_png,
                    model_dir=config.ncnn_model_dir,
                    model_name=effective_settings.model_name,
                    scale=effective_settings.scale,
                    tile_size=tile_size,
                    extra_args=parsed_extra_args,
                )
                return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
                runtime_failure_marker = _detect_ncnn_runtime_failure(stdout, stderr)
                corruption_detail = ""
                if return_code == 0:
                    corruption_detail = _detect_corrupt_ncnn_output(input_png, output_png)
                if return_code == 0 and not runtime_failure_marker and not corruption_detail:
                    succeeded = True
                    break
                if return_code != 0:
                    last_detail = stderr.strip() or stdout.strip() or f"Real-ESRGAN NCNN failed with exit code {return_code}"
                elif runtime_failure_marker:
                    last_detail = f"Real-ESRGAN NCNN reported a Vulkan/runtime failure marker: {runtime_failure_marker}"
                else:
                    last_detail = corruption_detail
                protective_retry = bool(runtime_failure_marker or corruption_detail)
                if attempt_index < len(attempt_tiles) and (config.retry_smaller_tile_on_failure or protective_retry):
                    next_tile = attempt_tiles[attempt_index]
                    if on_log:
                        if tile_size == 0:
                            on_log(
                                f"[{index}/{total}] NCNN attempt for {rel_display} produced invalid output; "
                                f"retrying in tiled mode with tile size {next_tile}."
                            )
                        else:
                            on_log(
                                f"[{index}/{total}] NCNN attempt for {rel_display} produced invalid output; "
                                f"retrying with smaller tile size {next_tile}."
                            )
                    continue
                break
            if not succeeded:
                raise ValueError(f"Real-ESRGAN NCNN failed for {rel_display}: {last_detail}")
            if correction_mode != UPSCALE_POST_CORRECTION_NONE:
                raise_if_cancelled(stop_event)
                correction_result = apply_post_upscale_color_correction(
                    input_png,
                    output_png,
                    correction_mode,
                    correction_plan=correction_plan,
                )
                if on_log and correction_result.applied:
                    on_log(f"[{index}/{total}] CORRECT {rel_display} [{texture_type}] -> {correction_result.detail}")
                elif on_log and correction_result.correction_action == "skip":
                    on_log(
                        f"[{index}/{total}] SKIP CORRECTION {rel_display} [{texture_type}] "
                        f"-> {correction_result.correction_reason}"
                    )

        if on_phase_progress:
            on_phase_progress(index, total, f"{index} / {total} PNG files")


def run_realesrgan_ncnn_stage(
    config: NormalizedConfig,
    *,
    processing_plan: Sequence[TextureProcessingPlan] = (),
    on_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if config.upscale_backend != UPSCALE_BACKEND_REALESRGAN_NCNN:
        return
    if config.ncnn_exe_path is None or config.ncnn_model_dir is None or not config.ncnn_model_name:
        raise ValueError("Real-ESRGAN NCNN is selected, but the executable, model folder, or model name is missing.")

    input_root = config.dds_staging_root if config.enable_dds_staging and config.dds_staging_root is not None else config.png_root
    if not input_root.exists() or not input_root.is_dir():
        raise ValueError(f"Real-ESRGAN NCNN input folder does not exist: {input_root}")

    if on_phase:
        on_phase("Upscaling", "Running Real-ESRGAN NCNN...", False)
    if on_log:
        on_log("Phase 1/2: running Real-ESRGAN NCNN.")
        on_log(f"Real-ESRGAN NCNN input folder: {input_root}")
    attempt_output_root = Path(tempfile.mkdtemp(prefix="cdmw_ncnn_"))
    try:
        _run_single_ncnn_attempt(
            config,
            input_root=input_root,
            output_root=attempt_output_root,
            processing_plan=processing_plan,
            on_log=on_log,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )
        if not config.dry_run:
            if on_log:
                on_log(f"Syncing Real-ESRGAN NCNN output back into PNG root: {config.png_root}")
            copy_mod_ready_loose_tree(
                attempt_output_root,
                config.png_root,
                overwrite=True,
                dry_run=False,
                on_log=None,
            )
        if on_log:
            on_log("Real-ESRGAN NCNN completed successfully.")
    finally:
        if attempt_output_root.exists():
            shutil.rmtree(attempt_output_root, ignore_errors=True)
