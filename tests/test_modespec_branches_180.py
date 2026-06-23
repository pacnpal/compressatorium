"""Guard for #180: the dispatch/consumer sites must read ModeSpec/registry
fields, not branch on tool/mode identity.

The design doc's §3.1 mapping table says every `mode.startswith(...)` /
`== "<modestring>"` capability check should become a `ModeSpec` field read. This
pins the greppable Acceptance (no such residual checks survive in `convert.py` /
`job_manager.py`) and documents the collapsed extension gate's coverage.
"""

import re
from pathlib import Path

import pytest

from routes.convert import _BAD_EXTENSION_REASON

_APP = Path(__file__).resolve().parent.parent / "app"

# The capability checks §3.1 says must become ModeSpec field reads. Note this is
# deliberately narrow: the kept `mode == "romz_extract"` (romz single-ROM
# validation) and `mode == "extractcd"` (rename helper) are NOT capability
# checks and must stay allowed.
_FORBIDDEN = re.compile(
    r'startswith\("(?:dolphin_|extract|z3ds_)"\)'
    r'|== "(?:z3ds_compress|dolphin_iso|dolphin_gcz)"'
)


@pytest.mark.parametrize("rel", ["routes/convert.py", "services/job_manager.py"])
def test_no_residual_capability_branches(rel):
    src = (_APP / rel).read_text(encoding="utf-8")
    hits = _FORBIDDEN.findall(src)
    assert not hits, f"residual tool/mode-identity capability branch in {rel}: {hits}"


def test_bad_extension_reason_covers_non_chdman_tools():
    # The collapsed extension gate is driven by this map. chdman is intentionally
    # absent (it validates by .chd presence, having dropped .chd from
    # input_extensions); makeps3iso (directory input, no suffix) is too.
    assert set(_BAD_EXTENSION_REASON) == {
        "dolphin", "z3ds", "nsz", "cso", "chain", "romz",
    }
    assert "chdman" not in _BAD_EXTENSION_REASON
