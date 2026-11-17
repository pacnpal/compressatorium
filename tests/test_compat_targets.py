"""The "Compatible with..." table (#429) against the tool registry.

Every mode in ``app/services/compat_targets.yml`` must be a real mode, and every
codec one that mode's picker offers (``registry.js``'s ``codecsForMode``) and
the backend accepts (``ModeSpec.codecs``, where the mode names them). A renamed
mode or codec then breaks this test instead of the picker. Every entry cites at
least one https source. The picker-codec half needs Node, like the registry
parity tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from routes.info import get_compat_targets
from services.compat_targets import compat_targets
from services.tools import registry

from tests.test_frontend_parity_186 import _eval_registry


def _entries():
    for target in compat_targets():
        for entry in target["entries"]:
            yield target, entry


def test_targets_are_unique_and_labelled():
    ids = [t["id"] for t in compat_targets()]
    assert len(ids) == len(set(ids))
    assert all(t["label"] and t["entries"] for t in compat_targets())


def test_every_entry_cites_a_source():
    for target, entry in _entries():
        sources = entry.get("sources") or []
        assert sources, f"{target['id']} {entry['modes']} has no source"
        assert all(s.startswith("https://") for s in sources), (target["id"], sources)


def test_modes_and_codecs_match_the_backend_registry():
    for target, entry in _entries():
        codecs = set(entry.get("codecs", []))
        assert set(entry.get("preferred", [])) <= codecs, target["id"]
        for mode in entry["modes"]:
            spec = registry.spec(mode)  # KeyError on an unknown mode
            if not codecs:
                continue
            assert spec.supports_compression, f"{target['id']}: {mode} takes no codec"
            if spec.codecs:
                assert codecs <= spec.codecs, (target["id"], mode, codecs - spec.codecs)


def test_codecs_are_ones_the_picker_offers(tmp_path: Path):
    modes = sorted({m for _, e in _entries() for m in e["modes"]})
    dump = (
        f"\nconst __modes = {json.dumps(modes)};\n"
        "process.stdout.write(JSON.stringify(__modes.map((m) => "
        "[m, registry.codecsForMode(m).map((c) => c.value)])));\n"
    )
    offered = dict(_eval_registry(tmp_path, dump, "compat_eval.mjs"))
    for target, entry in _entries():
        for mode in entry["modes"]:
            missing = set(entry.get("codecs", [])) - set(offered[mode])
            assert not missing, (target["id"], mode, missing)


@pytest.mark.asyncio
async def test_route_serves_the_table():
    assert await get_compat_targets() == {"targets": compat_targets()}
