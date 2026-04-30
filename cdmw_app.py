#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import sys
import time
import traceback
from typing import Callable, Optional, Sequence


def _bootstrap_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _write_bootstrap_report(kind: str, title: str, body: str) -> None:
    try:
        report_dir = _bootstrap_root() / "crash_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time() % 1) * 1000):03d}"
        report_path = report_dir / f"{kind}_{timestamp}_{os.getpid()}.log"
        lines = [
            "Crimson Desert Mod Workbench bootstrap report",
            f"Kind: {kind}",
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Process ID: {os.getpid()}",
            f"Python: {sys.version}",
            f"Platform: {platform.platform()}",
            "",
            title,
            "",
            body.rstrip(),
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Crimson Desert Mod Workbench")
    parser.add_argument("--cli", action="store_true", help="Run the command-line workflow using the top-level defaults.")
    parser.add_argument("--gui", action="store_true", help="Force the GUI workflow.")
    args = parser.parse_args(argv)

    if args.cli and args.gui:
        parser.error("Choose only one of --cli or --gui.")

    try:
        if args.cli:
            from cdmw.core.pipeline import run_cli

            runner: Callable[[], int] = run_cli
        else:
            from cdmw.ui.main_window import run_gui

            runner = run_gui
        return runner()
    except Exception:
        _write_bootstrap_report(
            "bootstrap_failure",
            "Application failed before the normal crash reporter completed startup",
            traceback.format_exc(),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
