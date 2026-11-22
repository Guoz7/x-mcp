"""Ensure unit and contract tests can import server, src, scripts, e2e, and tools."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _folder in ("src", "scripts", "e2e", "tools"):
    _path = str(ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
