#!/usr/bin/env python3
from __future__ import annotations

import argparse
from typing import Optional, Sequence

from cdmw.core.pipeline import run_cli
from cdmw.ui.main_window import run_gui


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Crimson Desert Mod Workbench")
    parser.add_argument("--cli", action="store_true", help="Run the command-line workflow using the top-level defaults.")
    parser.add_argument("--gui", action="store_true", help="Force the GUI workflow.")
    args = parser.parse_args(argv)

    if args.cli and args.gui:
        parser.error("Choose only one of --cli or --gui.")

    if args.cli:
        return run_cli()

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
