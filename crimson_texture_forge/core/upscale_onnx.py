from __future__ import annotations

import importlib
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Tuple, TypeAlias

if TYPE_CHECKING:
    from PIL.Image import Image as PilImage
    from typing import Protocol

    NumpyArray: TypeAlias = Any

    class OnnxValueInfoType(Protocol):
        name: str
        shape: Sequence[object] | None

    class InferenceSessionType(Protocol):
        def get_inputs(self) -> Sequence[OnnxValueInfoType]: ...

        def get_outputs(self) -> Sequence[OnnxValueInfoType]: ...

        def run(self, output_names: Sequence[str], input_feed: dict[str, object]) -> Sequence[NumpyArray]: ...
else:
    NumpyArray: TypeAlias = Any
    PilImage: TypeAlias = Any
    InferenceSessionType: TypeAlias = Any

def _load_optional_module(module_name: str) -> tuple[Any, Exception | None]:
    try:  # pragma: no cover - optional dependency path
        return importlib.import_module(module_name), None
    except Exception as exc:  # pragma: no cover - optional dependency path
        return None, exc

from crimson_texture_forge.constants import UPSCALE_BACKEND_ONNX_RUNTIME, UPSCALE_POST_CORRECTION_NONE
from crimson_texture_forge.core.common import raise_if_cancelled
from crimson_texture_forge.core.upscale_postprocess import (
    apply_post_upscale_color_correction,
    describe_post_upscale_correction_mode,
    should_apply_post_upscale_correction,
)
from crimson_texture_forge.core.upscale_profiles import (
    build_ncnn_retry_tile_candidates,
    classify_texture_type,
    copy_mod_ready_loose_tree,
    describe_texture_preset,
    should_upscale_texture,
)
from crimson_texture_forge.models import NormalizedConfig

np, _numpy_import_error = _load_optional_module("numpy")
Image, _pil_import_error = _load_optional_module("PIL.Image")
ort, _onnxruntime_import_error = _load_optional_module("onnxruntime")


def is_onnxruntime_available() -> bool:
    return np is not None and Image is not None and ort is not None


def onnxruntime_error_message() -> str:
    parts: List[str] = []
    if _numpy_import_error is not None:
        parts.append(f"numpy is not available: {_numpy_import_error}")
    if _pil_import_error is not None:
        parts.append(f"Pillow is not available: {_pil_import_error}")
    if _onnxruntime_import_error is not None:
        parts.append(f"onnxruntime is not available: {_onnxruntime_import_error}")
    return " | ".join(parts)


def available_providers() -> Tuple[str, ...]:
    if ort is None:
        return ()
    try:
        return tuple(ort.get_available_providers())
    except Exception:
        return ("CPUExecutionProvider",)


def discover_onnx_models(model_dir: Path) -> List[Path]:
    resolved = Path(model_dir)
    if not resolved.exists() or not resolved.is_dir():
        return []
    return sorted(path for path in resolved.glob("*.onnx") if path.is_file())


def resolve_onnx_model_dir(explicit_model_dir: Optional[Path]) -> Optional[Path]:
    if explicit_model_dir is None:
        return None
    return explicit_model_dir


@dataclass(slots=True)
class OnnxModelInfo:
    model_path: Path
    input_name: str
    output_name: str
    input_channels: int
    output_channels: int
    providers: Tuple[str, ...]
    available_providers: Tuple[str, ...]
    input_shape: Tuple[object, ...]
    output_shape: Tuple[object, ...]


@dataclass(slots=True)
class OnnxUpscaleResult:
    source_path: Path
    output_path: Path
    scale: int
    tile_size: int
    source_size: Tuple[int, int]
    output_size: Tuple[int, int]
    preserved_alpha: bool


def _require_image_stack() -> None:
    if np is None or Image is None:
        raise RuntimeError(onnxruntime_error_message() or "numpy or Pillow is not available.")


def _require_onnxruntime() -> None:
    if ort is None:
        raise RuntimeError(onnxruntime_error_message() or "onnxruntime is not available.")


def _numpy_module() -> Any:
    _require_image_stack()
    return np


def _pil_image_module() -> Any:
    _require_image_stack()
    return Image


def _onnxruntime_module() -> Any:
    _require_onnxruntime()
    return ort


def _resampling_filter(name: str) -> int:
    image_module = _pil_image_module()
    resampling = getattr(image_module, "Resampling", image_module)
    return getattr(resampling, name)


def _image_mode_to_channels(mode: str) -> int:
    normalized = (mode or "").upper()
    if normalized in {"L", "P"}:
        return 1
    if normalized in {"RGB"}:
        return 3
    if normalized in {"RGBA"}:
        return 4
    return 3


def _image_to_array(image: PilImage, channels: int) -> NumpyArray:
    numpy_module = _numpy_module()
    if channels == 1:
        working = image.convert("L")
        data = numpy_module.asarray(working, dtype=numpy_module.float32)[..., None]
    elif channels == 4:
        working = image.convert("RGBA")
        data = numpy_module.asarray(working, dtype=numpy_module.float32)
    else:
        working = image.convert("RGB")
        data = numpy_module.asarray(working, dtype=numpy_module.float32)
    return numpy_module.transpose(data / 255.0, (2, 0, 1))[None, ...]


def _array_to_image(array: NumpyArray) -> PilImage:
    numpy_module = _numpy_module()
    image_module = _pil_image_module()
    if array.ndim != 4 or array.shape[0] != 1:
        raise ValueError(f"Expected a single-image NCHW tensor, got shape {array.shape!r}.")
    channels = array.shape[1]
    data = numpy_module.clip(array[0], 0.0, 1.0)
    data = numpy_module.transpose(data, (1, 2, 0))
    if channels == 1:
        data = data[..., 0]
        mode = "L"
    elif channels == 4:
        mode = "RGBA"
    else:
        mode = "RGB"
    return image_module.fromarray((data * 255.0 + 0.5).astype(numpy_module.uint8), mode=mode)


class EsrganOnnxUpscaler:
    def __init__(
        self,
        model_path: Path,
        *,
        providers: Optional[Sequence[str]] = None,
    ) -> None:
        _require_image_stack()
        _require_onnxruntime()
        self.model_path = Path(model_path)
        if not self.model_path.exists() or not self.model_path.is_file():
            raise ValueError(f"ONNX model file does not exist: {self.model_path}")

        self.available_providers = available_providers()
        requested = list(providers or ("CPUExecutionProvider",))
        resolved_providers = [provider for provider in requested if provider in self.available_providers]
        if not resolved_providers and self.available_providers:
            resolved_providers = [self.available_providers[0]]
        if not resolved_providers:
            raise RuntimeError("No usable onnxruntime execution providers were found.")

        self.providers = tuple(resolved_providers)
        ort_module = _onnxruntime_module()
        self.session: InferenceSessionType = ort_module.InferenceSession(str(self.model_path), providers=list(self.providers))

        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError(
                f"ESRGAN-style helper expects exactly one input and one output tensor, "
                f"found {len(inputs)} input(s) and {len(outputs)} output(s)."
            )

        self.input_name = inputs[0].name
        self.output_name = outputs[0].name
        self.input_shape = tuple(inputs[0].shape or ())
        self.output_shape = tuple(outputs[0].shape or ())
        self.input_channels = self._infer_channels(self.input_shape)
        self.output_channels = self._infer_channels(self.output_shape)
        if self.input_channels not in {1, 3, 4}:
            raise ValueError(
                f"Unsupported ONNX input channels: {self.input_channels or 'unknown'} "
                f"for shape {self.input_shape!r}. Supported ESRGAN-style inputs are 1, 3, or 4 channels."
            )
        if self.output_channels not in {1, 3, 4}:
            raise ValueError(
                f"Unsupported ONNX output channels: {self.output_channels or 'unknown'} "
                f"for shape {self.output_shape!r}. Supported ESRGAN-style outputs are 1, 3, or 4 channels."
            )

    @classmethod
    def from_model_path(
        cls,
        model_path: Path,
        *,
        providers: Optional[Sequence[str]] = None,
    ) -> "EsrganOnnxUpscaler":
        return cls(model_path, providers=providers)

    @staticmethod
    def _infer_channels(shape: Sequence[object]) -> int:
        if len(shape) != 4:
            return 0
        channels = shape[1]
        if isinstance(channels, int) and channels > 0:
            return channels
        return 0

    def describe_model(self) -> OnnxModelInfo:
        return OnnxModelInfo(
            model_path=self.model_path,
            input_name=self.input_name,
            output_name=self.output_name,
            input_channels=self.input_channels,
            output_channels=self.output_channels,
            providers=self.providers,
            available_providers=self.available_providers,
            input_shape=self.input_shape,
            output_shape=self.output_shape,
        )

    def _run_tensor(self, tensor: NumpyArray) -> NumpyArray:
        outputs = self.session.run([self.output_name], {self.input_name: tensor})
        if not outputs:
            raise RuntimeError("ONNX Runtime returned no outputs.")
        output = outputs[0]
        if output.ndim != 4:
            raise ValueError(f"Unsupported ONNX output shape: {output.shape!r}")
        return output

    def _infer_scale(self, input_tensor: NumpyArray, output_tensor: NumpyArray) -> int:
        in_h = int(input_tensor.shape[2])
        in_w = int(input_tensor.shape[3])
        out_h = int(output_tensor.shape[2])
        out_w = int(output_tensor.shape[3])
        if in_h <= 0 or in_w <= 0:
            raise ValueError("Invalid input tensor size.")
        if out_h % in_h != 0 or out_w % in_w != 0:
            raise ValueError(f"Model output size {out_w}x{out_h} is not an integer scale of {in_w}x{in_h}.")
        scale_h = out_h // in_h
        scale_w = out_w // in_w
        if scale_h != scale_w:
            raise ValueError(f"Non-uniform ONNX output scaling is not supported ({scale_w}x{scale_h}).")
        if scale_h <= 0:
            raise ValueError("Derived invalid output scale.")
        return scale_h

    def _prepare_input(self, image: PilImage) -> Tuple[NumpyArray, bool]:
        has_alpha = image.mode in {"RGBA", "LA"} or "A" in image.getbands()
        if self.input_channels == 4:
            return _image_to_array(image.convert("RGBA"), 4), has_alpha
        if self.input_channels == 1:
            return _image_to_array(image.convert("L"), 1), has_alpha
        return _image_to_array(image.convert("RGB"), 3), has_alpha

    def _prepare_alpha(self, image: PilImage, size: Tuple[int, int]) -> Optional[PilImage]:
        if "A" not in image.getbands():
            return None
        alpha = image.getchannel("A")
        return alpha.resize(size, _resampling_filter("BICUBIC"))

    def _run_full_frame(self, image: PilImage) -> Tuple[PilImage, int, bool]:
        tensor, has_alpha = self._prepare_input(image)
        output = self._run_tensor(tensor)
        scale = self._infer_scale(tensor, output)
        output_image = _array_to_image(output)

        if has_alpha and output_image.mode != "RGBA":
            alpha = self._prepare_alpha(image, output_image.size)
            if alpha is not None:
                if output_image.mode != "RGB":
                    output_image = output_image.convert("RGB")
                output_image.putalpha(alpha)
        return output_image, scale, has_alpha

    def _run_tiled_frame(self, image: PilImage, tile_size: int, tile_overlap: int) -> Tuple[PilImage, int, bool]:
        if tile_size <= 0:
            return self._run_full_frame(image)

        working = image
        if self.input_channels == 1:
            working = image.convert("L")
        elif self.input_channels == 4 and ("A" in image.getbands()):
            working = image.convert("RGBA")
        else:
            working = image.convert("RGB")

        width, height = working.size
        tile_size = max(1, min(tile_size, width, height))
        pad = max(0, min(tile_overlap, tile_size // 2))
        step = max(1, tile_size - pad * 2)

        probe_left = 0
        probe_top = 0
        probe_right = min(width, tile_size + pad)
        probe_bottom = min(height, tile_size + pad)
        probe_tensor = _image_to_array(working.crop((probe_left, probe_top, probe_right, probe_bottom)), self.input_channels or _image_mode_to_channels(working.mode))
        probe_output = self._run_tensor(probe_tensor)
        scale = self._infer_scale(probe_tensor, probe_output)

        output_channels = probe_output.shape[1]
        output_mode = "RGBA" if output_channels == 4 else "RGB" if output_channels == 3 else "L"
        image_module = _pil_image_module()
        canvas = image_module.new(output_mode, (width * scale, height * scale))

        for top in range(0, height, step):
            for left in range(0, width, step):
                core_right = min(width, left + tile_size)
                core_bottom = min(height, top + tile_size)
                crop_left = max(0, left - pad)
                crop_top = max(0, top - pad)
                crop_right = min(width, core_right + pad)
                crop_bottom = min(height, core_bottom + pad)
                tile = working.crop((crop_left, crop_top, crop_right, crop_bottom))
                tensor = _image_to_array(tile, self.input_channels or _image_mode_to_channels(working.mode))
                output = self._run_tensor(tensor)
                tile_image = _array_to_image(output)

                left_pad = left - crop_left
                top_pad = top - crop_top
                right_pad = crop_right - core_right
                bottom_pad = crop_bottom - core_bottom
                crop_box = (
                    left_pad * scale,
                    top_pad * scale,
                    tile_image.width - right_pad * scale,
                    tile_image.height - bottom_pad * scale,
                )
                core_image = tile_image.crop(crop_box)
                canvas.paste(core_image, (left * scale, top * scale))

        has_alpha = "A" in image.getbands()
        if has_alpha and canvas.mode != "RGBA":
            alpha = self._prepare_alpha(image, canvas.size)
            if alpha is not None:
                if canvas.mode != "RGB":
                    canvas = canvas.convert("RGB")
                canvas.putalpha(alpha)
        return canvas, scale, has_alpha

    def upscale_image(
        self,
        image: PilImage,
        *,
        tile_size: int = 0,
        tile_overlap: int = 16,
    ) -> Tuple[PilImage, OnnxUpscaleResult]:
        if tile_size > 0:
            output_image, scale, has_alpha = self._run_tiled_frame(image, tile_size, tile_overlap)
        else:
            output_image, scale, has_alpha = self._run_full_frame(image)
        result = OnnxUpscaleResult(
            source_path=Path(),
            output_path=Path(),
            scale=scale,
            tile_size=max(0, int(tile_size)),
            source_size=image.size,
            output_size=output_image.size,
            preserved_alpha=has_alpha,
        )
        return output_image, result

    def upscale_png(
        self,
        input_path: Path,
        output_path: Path,
        *,
        tile_size: int = 0,
        tile_overlap: int = 16,
    ) -> OnnxUpscaleResult:
        source = Path(input_path)
        destination = Path(output_path)
        if not source.exists() or not source.is_file():
            raise ValueError(f"Input PNG does not exist: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        image_module = _pil_image_module()
        with image_module.open(source) as image:
            output_image, result = self.upscale_image(image, tile_size=tile_size, tile_overlap=tile_overlap)
            output_image.save(destination, format="PNG")
        return OnnxUpscaleResult(
            source_path=source,
            output_path=destination,
            scale=result.scale,
            tile_size=result.tile_size,
            source_size=result.source_size,
            output_size=result.output_size,
            preserved_alpha=result.preserved_alpha,
        )


def _preferred_providers() -> Tuple[str, ...]:
    providers = available_providers()
    if not providers:
        return ("CPUExecutionProvider",)
    preferred_order = (
        "DmlExecutionProvider",
        "CUDAExecutionProvider",
        "CoreMLExecutionProvider",
        "CPUExecutionProvider",
    )
    resolved: List[str] = []
    for provider in preferred_order:
        if provider in providers and provider not in resolved:
            resolved.append(provider)
    for provider in providers:
        if provider not in resolved:
            resolved.append(provider)
    return tuple(resolved)


def _run_single_onnx_attempt(
    config: NormalizedConfig,
    *,
    input_root: Path,
    output_root: Path,
    tile_size: int,
    on_log: Optional[Callable[[str], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    png_inputs = sorted(path for path in input_root.rglob("*.png") if path.is_file())
    total = len(png_inputs)
    if total == 0:
        raise ValueError(
            f"No PNG files were found for ONNX Runtime in {input_root}. "
            "Enable DDS staging first or populate PNG root with source PNG files."
        )

    if config.onnx_model_dir is None or not config.onnx_model_name:
        raise ValueError("ONNX Runtime is selected, but the ONNX model folder or model name is missing.")

    model_path = config.onnx_model_dir / f"{config.onnx_model_name}.onnx"
    if not model_path.exists() or not model_path.is_file():
        raise ValueError(f"Selected ONNX model does not exist: {model_path}")

    providers = _preferred_providers()
    upscaler = EsrganOnnxUpscaler.from_model_path(model_path, providers=providers)
    model_info = upscaler.describe_model()

    if on_log:
        on_log(f"ONNX model file: {model_info.model_path}")
        on_log(f"ONNX providers: {', '.join(model_info.providers) or 'none'}")
        on_log(f"ONNX available providers: {', '.join(model_info.available_providers) or 'none'}")
        on_log(f"ONNX tile={tile_size}, preset={config.upscale_texture_preset}")
        on_log(f"ONNX post correction={describe_post_upscale_correction_mode(config.upscale_post_correction_mode)}")
        on_log(describe_texture_preset(config.upscale_texture_preset))

    if on_phase_progress:
        on_phase_progress(0, total, f"0 / {total} PNG files")

    inferred_scale: Optional[int] = None
    for index, input_png in enumerate(png_inputs, start=1):
        raise_if_cancelled(stop_event)
        rel_path = input_png.relative_to(input_root)
        rel_display = rel_path.as_posix()
        texture_type = classify_texture_type(rel_display)
        output_png = output_root / rel_path
        output_png.parent.mkdir(parents=True, exist_ok=True)
        if on_current_file:
            on_current_file(f"Upscale: {rel_display}")

        if should_upscale_texture(texture_type, config.upscale_texture_preset):
            action = "DRYRUN" if config.dry_run else "UPSCALE"
            if on_log:
                on_log(f"[{index}/{total}] {action} {rel_display} [{texture_type}]")
            if not config.dry_run:
                result = upscaler.upscale_png(input_png, output_png, tile_size=tile_size)
                if config.upscale_post_correction_mode != UPSCALE_POST_CORRECTION_NONE:
                    raise_if_cancelled(stop_event)
                    if should_apply_post_upscale_correction(texture_type):
                        correction_result = apply_post_upscale_color_correction(
                            input_png,
                            output_png,
                            config.upscale_post_correction_mode,
                        )
                        if on_log and correction_result.applied:
                            on_log(f"[{index}/{total}] CORRECT {rel_display} [{texture_type}] -> {correction_result.detail}")
                    elif on_log:
                        on_log(
                            f"[{index}/{total}] SKIP CORRECTION {rel_display} [{texture_type}] "
                            "-> limited to visible color, UI, emissive, and impostor textures."
                        )
                if inferred_scale is None:
                    inferred_scale = result.scale
                    if int(config.ncnn_scale) > 0 and inferred_scale != int(config.ncnn_scale):
                        raise ValueError(
                            f"Selected ONNX model scales by {inferred_scale}x, but the workflow scale is set to {config.ncnn_scale}x."
                        )
        else:
            action = "DRYRUN COPY" if config.dry_run else "COPY"
            if on_log:
                on_log(f"[{index}/{total}] {action} {rel_display} [{texture_type}] -> preset keeps source PNG")
            if not config.dry_run:
                shutil.copy2(input_png, output_png)

        if on_phase_progress:
            on_phase_progress(index, total, f"{index} / {total} PNG files")


def run_onnx_stage(
    config: NormalizedConfig,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if config.upscale_backend != UPSCALE_BACKEND_ONNX_RUNTIME:
        return
    if not is_onnxruntime_available():
        raise ValueError(onnxruntime_error_message() or "onnxruntime is not available.")

    input_root = config.dds_staging_root if config.enable_dds_staging and config.dds_staging_root is not None else config.png_root
    if not input_root.exists() or not input_root.is_dir():
        raise ValueError(f"ONNX Runtime input folder does not exist: {input_root}")

    retry_plan = build_ncnn_retry_tile_candidates(config.ncnn_tile_size, include_full_frame_fallback=True)
    candidate_tiles = retry_plan.candidate_tile_sizes or (max(0, int(config.ncnn_tile_size)),)
    attempt_tiles = candidate_tiles if config.retry_smaller_tile_on_failure and not config.dry_run else (candidate_tiles[0],)
    total_attempts = len(attempt_tiles)
    last_error: Optional[Exception] = None

    if on_phase:
        on_phase("Upscaling", "Running ONNX Runtime...", False)
    if on_log:
        on_log("Phase 1/2: running ONNX Runtime.")
        on_log(f"ONNX Runtime input folder: {input_root}")
        on_log(f"ONNX retry tile candidates: {', '.join(str(tile) for tile in attempt_tiles)}")

    for attempt_index, tile_size in enumerate(attempt_tiles, start=1):
        attempt_output_root = Path(tempfile.mkdtemp(prefix="crimson_texture_forge_onnx_"))
        try:
            if on_log and total_attempts > 1:
                on_log(f"ONNX attempt {attempt_index}/{total_attempts} using tile size {tile_size}.")
            _run_single_onnx_attempt(
                config,
                input_root=input_root,
                output_root=attempt_output_root,
                tile_size=tile_size,
                on_log=on_log,
                on_phase_progress=on_phase_progress,
                on_current_file=on_current_file,
                stop_event=stop_event,
            )
            if not config.dry_run:
                if on_log:
                    on_log(f"Syncing ONNX Runtime output back into PNG root: {config.png_root}")
                copy_mod_ready_loose_tree(
                    attempt_output_root,
                    config.png_root,
                    overwrite=True,
                    dry_run=False,
                    on_log=None,
                )
            if on_log:
                on_log("ONNX Runtime completed successfully.")
            return
        except Exception as exc:
            last_error = exc
            if on_log:
                on_log(f"ONNX attempt with tile {tile_size} failed: {exc}")
            if attempt_index < total_attempts and on_log:
                on_log(f"Retrying ONNX Runtime with smaller tile size {attempt_tiles[attempt_index]}.")
        finally:
            if attempt_output_root.exists():
                shutil.rmtree(attempt_output_root, ignore_errors=True)

    if last_error is not None:
        raise last_error
