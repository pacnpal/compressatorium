"""A test that patches a registry tool instance leaves no trace (#306).

``monkeypatch.setattr(tool, "verify", fake)`` on an instance whose ``verify``
comes from its class records the class's bound method as the old value, and
its undo *sets* that bound method on the instance instead of deleting the
attribute. The registry's tools are process-wide singletons, so a later test
that patches the class (``monkeypatch.setattr(type(tool), "verify", ...)``)
is silently shadowed. tests/test_referrers_m3u.py did this to
tests/test_cli_306.py's deadline test when both ran in one process. The
autouse fixture in conftest.py restores every tool's instance attributes.

The two tests below run in file order: the first leaves the pin behind the
way the old fixture did, the second must still see its class patch.
"""
from __future__ import annotations

from services.tools import registry


def test_a_patch_on_a_tool_instance(monkeypatch):
    tool = registry.get("dolphin")

    async def fake(path, *, cancel_event=None):
        return {"valid": True, "message": "fake"}

    monkeypatch.setattr(tool, "verify", fake, raising=False)
    assert tool.verify is fake


def test_a_later_class_patch_is_not_shadowed(monkeypatch):
    tool = registry.get("dolphin")
    assert "verify" not in vars(tool), "an earlier test pinned verify on the instance"

    async def stuck(self, path, *, cancel_event=None):
        return {"valid": False, "message": "class patch"}

    monkeypatch.setattr(type(tool), "verify", stuck)
    assert tool.verify.__func__ is stuck
