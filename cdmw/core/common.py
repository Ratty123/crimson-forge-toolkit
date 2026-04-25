from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cdmw.models import RunCancelled

def raise_if_cancelled(stop_event: Optional[threading.Event], message: str = "Processing stopped by user.") -> None:
    if stop_event and stop_event.is_set():
        raise RunCancelled(message)


def run_process_with_cancellation(
    cmd: Sequence[str],
    stop_event: Optional[threading.Event] = None,
    env_overrides: Optional[Dict[str, Optional[str]]] = None,
    on_poll: Optional[Callable[[], None]] = None,
    on_cancel: Optional[Callable[[subprocess.Popen], None]] = None,
) -> Tuple[int, str, str]:
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env: Optional[Dict[str, str]] = None
    if env_overrides:
        env = dict(os.environ)
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        env=env,
    )

    try:
        while True:
            raise_if_cancelled(stop_event)
            if on_poll:
                on_poll()
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                if on_poll:
                    on_poll()
                return proc.returncode, stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                continue
    except RunCancelled:
        if on_cancel is not None:
            try:
                on_cancel(proc)
            except Exception:
                pass
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise RunCancelled("Processing stopped by user.")


def split_log_lines(text: str) -> List[str]:
    return [line.rstrip() for line in text.replace("\r", "\n").split("\n") if line.strip()]


def sleep_with_cancellation(seconds: float, stop_event: Optional[threading.Event]) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        raise_if_cancelled(stop_event)
        time.sleep(min(0.2, deadline - time.monotonic()))


def read_u32_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)

