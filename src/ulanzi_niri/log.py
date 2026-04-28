"""Logging setup."""

from __future__ import annotations

import logging
import os


def configure(level: str | None = None) -> None:
    lvl = (level or os.environ.get("ULANZI_NIRI_LOG") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
