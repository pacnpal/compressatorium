"""The "Compatible with..." table (#429): which codecs each emulator reads.

Read from ``app/data/compat_presets.yaml``, the one place the table lives. The
routes serve it as is and the frontend keeps no copy. A malformed file raises
at load, and ``tests/test_compat_presets_429.py`` checks every mode and codec
in it against the tool registry.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

PRESETS_FILE = Path(__file__).resolve().parent.parent / "data" / "compat_presets.yaml"


def _validate(table: object) -> dict:
    if not isinstance(table, dict) or not isinstance(table.get("targets"), list):
        raise ValueError("compat presets: expected a top-level `targets` list")
    seen: set[str] = set()
    for target in table["targets"]:
        tid = target.get("id")
        if not tid or not target.get("label") or tid in seen:
            raise ValueError(f"compat presets: target needs a unique id and a label: {target}")
        seen.add(tid)
        if not target.get("entries"):
            raise ValueError(f"compat presets: {tid} has no entries")
        for entry in target["entries"]:
            where = f"{tid}/{entry.get('mode')}"
            if not entry.get("mode"):
                raise ValueError(f"compat presets: {tid} entry has no mode")
            for key in ("codecs", "preferred"):
                if not isinstance(entry.get(key), list) or not entry[key]:
                    raise ValueError(f"compat presets: {where} needs a non-empty `{key}` list")
            parsed = urlparse(str(entry.get("source") or ""))
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"compat presets: {where} needs an https `source` URL")
    return table


def load_presets(path: Path = PRESETS_FILE) -> dict:
    """Parse and check the presets file; raises ``ValueError`` when malformed."""
    with open(path, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh))


@lru_cache(maxsize=1)
def cached_presets() -> dict:
    """The shipped table, loaded once per process."""
    return load_presets()


def served_presets() -> dict:
    """The table plus ``implicit_codecs``: mode -> the codecs its tool uses when
    none is chosen, read from the mode specs so no list is kept twice."""
    from services.tools import registry

    implicit = {
        spec.mode: list(codecs)
        for spec in registry.mode_specs()
        if (codecs := getattr(spec, "implicit_codecs", ()))
    }
    return {**cached_presets(), "implicit_codecs": implicit}
