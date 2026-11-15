"""ExodosTool: the plugin over services.exodos, and its registry wiring."""
from __future__ import annotations

import asyncio
import zipfile

import pytest
from services import exodos as exodos_module
from services.tools import registry
from services.tools.spec import ModeKind

from app.routes import convert as convert_routes


@pytest.fixture
def tree(tmp_path, monkeypatch):
    dos = tmp_path / "!dos"
    (dos / "Abaron").mkdir(parents=True)
    (dos / "Abaron" / "dosbox.conf").write_bytes(b"[autoexec]\r\nmount c .\\eXoDOS\\Abaron\r\n")
    monkeypatch.setattr(exodos_module.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", str(dos))
    game = tmp_path / "Abaron (1995).zip"
    with zipfile.ZipFile(game, "w") as zf:
        zf.writestr("Abaron/ABARON.EXE", b"exe")
    return {"root": tmp_path, "game": str(game)}


def test_the_mode_belongs_to_the_exodos_tool():
    assert registry.for_mode("exodos_repack").id == "exodos"


def test_the_mode_keeps_its_source_and_says_so():
    spec = registry.spec("exodos_repack")

    assert spec.kind is ModeKind.COMPRESS
    assert spec.input_extensions == frozenset({".zip"})
    assert spec.output_ext == ".dosz"
    assert spec.supports_verify is True
    assert spec.supports_delete_on_verify is False
    assert spec.supports_round_trip is False
    assert spec.allows_archive_input is False


def test_the_tool_serves_romms_dos_platform():
    assert registry.get("exodos").platform_slugs == frozenset({"dos"})


def test_only_repackable_zips_are_offered(tree, tmp_path):
    tool = registry.get("exodos")
    loose = tmp_path / "Loose.zip"
    with zipfile.ZipFile(loose, "w") as zf:
        zf.writestr("README.TXT", b"x")

    assert tool.converts_path(tree["game"]) is True
    assert tool.converts_path(str(loose)) is False


def test_the_tool_is_ready_only_with_a_template_tree(tree, monkeypatch):
    tool = registry.get("exodos")

    assert asyncio.run(tool.is_ready()) is True
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", None)
    assert asyncio.run(tool.is_ready()) is False


def test_the_product_verifies_through_this_tool():
    assert registry.tool_for_verify("Abaron (1995).dosz").id == "exodos"


def test_plan_reasons_are_the_tools_own():
    assert convert_routes._BAD_EXTENSION_REASON["exodos"] is (
        convert_routes.SkipReason.EXODOS_BAD_EXTENSION
    )
    assert convert_routes._NOT_CONVERTIBLE_REASON["exodos"] is (
        convert_routes.SkipReason.EXODOS_NOT_REPACKABLE
    )


def test_rules_can_never_run_the_repack():
    """Manual only: no Rule may run it, however the rule arrives."""
    from services.romm import auto as romm_auto

    assert registry.spec("exodos_repack").automatable is False
    assert registry.mode_is_automatable("exodos_repack") is False
    assert romm_auto.normalize_rule({"mode": "exodos_repack", "enabled": True}) is None
    assert romm_auto.normalize_rules({"9": {"mode": "exodos_repack", "enabled": True}}) == {}


def test_every_other_mode_keeps_the_default():
    assert all(
        spec.automatable for spec in registry.mode_specs() if spec.mode != "exodos_repack"
    )
