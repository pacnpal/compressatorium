"""The "Compatible with..." table (#429): which modes and codecs each target reads.

The data lives in ``compat_targets.yml`` beside this module, one cited entry per
claim, and is served unchanged by ``GET /api/compat-targets`` so the frontend
holds no copy of it.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

COMPAT_TARGETS_PATH = Path(__file__).with_name("compat_targets.yml")


@cache
def compat_targets() -> list[dict]:
    """The targets from ``compat_targets.yml``, read once."""
    with COMPAT_TARGETS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)["targets"]
