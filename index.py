"""Entrypoint FastAPI zero-config para deploy serverless na Vercel."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

for path_str in (str(SRC), str(ROOT)):
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from analista_ia.web_server import app  # noqa: E402

__all__ = ["app"]
