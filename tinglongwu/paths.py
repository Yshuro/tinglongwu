"""Central app paths (#1664). Stdlib only: `tinglongwu/__init__` imports offline first, and offline may import this.

APP_ROOT: the folder that holds the `tinglongwu/` package -- the repo root in a source checkout, the install dir
({app}, next to tlwu.exe) in the Nuitka standalone build. DATA_DIR: the package's bundled `data/` folder.
When the package moves (src/ layout), only this file changes.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
APP_ROOT = PACKAGE_DIR.parent
DATA_DIR = PACKAGE_DIR / "data"
