#!/usr/bin/env python3
"""Prepare evbug / PCI NE2000 paired experiment inputs without model or build calls."""

import sys
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent / "native_experiment"),
]
from public_prepare import main

if __name__ == "__main__":
    main()
