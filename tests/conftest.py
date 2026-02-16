"""Pytest configuration: make `scripts/` importable so tests can do
`from _common import ...` without rearranging the repo layout."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
