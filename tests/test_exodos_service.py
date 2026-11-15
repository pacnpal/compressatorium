"""services.exodos.ExodosService: gating, zip inspection, confinement, output.

Synthetic fixtures only: a tiny eXoDOS-shaped tree under a temp Volume.
"""
from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import pytest
from services import exodos as exodos_module
from services.exodos import ExodosService, NotRepackable
from services.subprocess_runner import ConversionCancelled

TEMPLATE = (
    b"[autoexec]\r\n"
    b"mount c .\\eXoDOS\\Abaron\r\n"
    b"imgmount d .\\eXoDOS\\Abaron\\CD\\ABARON.CUE -t cdrom\r\n"
    b"c:\r\n"
    b"ABARON.EXE\r\n"
)


def _zip(path, names):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, b"" if name.endswith("/") else f"data:{name}".encode())
    return str(path)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    dos = volume / "eXo" / "eXoDOS" / "!dos"
    (dos / "Abaron").mkdir(parents=True)
    (dos / "Abaron" / "dosbox.conf").write_bytes(TEMPLATE)
    games = volume / "eXo" / "eXoDOS"
    monkeypatch.setattr(exodos_module.settings, "chd_volumes", str(volume))
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", str(dos))
    monkeypatch.setattr(exodos_module.settings, "archive_max_entries", 0)
    return {"volume": volume, "dos": dos, "games": games}


def _game_zip(tree, names=None, name="Abaron (1995).zip"):
    names = names or ["Abaron/", "Abaron/ABARON.EXE", "Abaron/CD/ABARON.CUE"]
    return _zip(tree["games"] / name, names)


# --- gating ----------------------------------------------------------------


def test_the_template_tree_comes_from_exodos_dos_dir(tree):
    assert ExodosService().dos_dir() == os.path.realpath(tree["dos"])


def test_no_template_tree_when_the_variable_is_unset(tree, monkeypatch):
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", None)

    assert ExodosService().dos_dir() is None


def test_a_template_tree_outside_every_volume_is_refused(tree, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "!dos"
    outside.mkdir(parents=True)
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", str(outside))

    assert ExodosService().dos_dir() is None


def test_a_missing_template_tree_is_refused(tree, monkeypatch):
    monkeypatch.setattr(
        exodos_module.settings, "exodos_dos_dir", str(tree["volume"] / "nope"),
    )

    assert ExodosService().dos_dir() is None


# --- inspection ------------------------------------------------------------


def test_inspect_finds_the_game_folder_and_its_template(tree):
    plan = ExodosService().inspect(_game_zip(tree))

    assert plan.top == "Abaron"
    assert plan.template == os.path.realpath(tree["dos"] / "Abaron" / "dosbox.conf")


@pytest.mark.parametrize(
    ("names", "reason"),
    [
        (["ABARON.EXE"], "single game folder"),
        (["Abaron/A.EXE", "Other/B.EXE"], "single game folder"),
        (["Abaron/"], "single game folder"),
        (["Abaron", "Abaron/A.EXE"], "single game folder"),
        (["Abaron/A.EXE", "Abaron/../../evil"], "Invalid archive member"),
        (["/abs/A.EXE"], "Absolute paths"),
        (["Abaron/C:/A.EXE"], "Drive letters"),
    ],
)
def test_inspect_refuses_zips_that_are_not_one_safe_game_folder(tree, names, reason):
    path = _game_zip(tree, names)

    with pytest.raises(NotRepackable, match=reason):
        ExodosService().inspect(path)


def test_inspect_refuses_a_game_with_no_template(tree):
    path = _game_zip(tree, ["Zork/ZORK.EXE"], name="Zork.zip")

    with pytest.raises(NotRepackable, match="no template"):
        ExodosService().inspect(path)


def test_inspect_refuses_a_template_that_resolves_outside_the_tree(tree, tmp_path):
    secret = tmp_path / "secret.conf"
    secret.write_bytes(b"[autoexec]\r\n")
    (tree["dos"] / "Zork").mkdir()
    (tree["dos"] / "Zork" / "dosbox.conf").symlink_to(secret)
    path = _game_zip(tree, ["Zork/ZORK.EXE"], name="Zork.zip")

    with pytest.raises(NotRepackable, match="outside"):
        ExodosService().inspect(path)


def test_inspect_applies_the_archive_entry_limit(tree, monkeypatch):
    monkeypatch.setattr(exodos_module.settings, "archive_max_entries", 2)

    with pytest.raises(NotRepackable, match="max entries"):
        ExodosService().inspect(_game_zip(tree))


def test_inspect_refuses_when_no_template_tree_is_configured(tree, monkeypatch):
    path = _game_zip(tree)
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", None)

    with pytest.raises(NotRepackable, match="EXODOS_DOS_DIR"):
        ExodosService().inspect(path)


def test_is_repackable_answers_without_raising(tree):
    service = ExodosService()

    assert service.is_repackable(_game_zip(tree)) is True
    assert service.is_repackable(_game_zip(tree, ["X.EXE"], name="Loose.zip")) is False
    assert service.is_repackable(str(tree["games"] / "missing.zip")) is False


def test_only_zips_are_repackable(tree):
    path = _game_zip(tree)
    renamed = path.replace(".zip", ".7z")
    os.rename(path, renamed)

    assert ExodosService().is_repackable(renamed) is False


# --- output path -----------------------------------------------------------


def test_the_product_sits_beside_the_source_as_dosz(tree):
    source = str(tree["games"] / "Abaron (1995).zip")

    assert ExodosService().get_output_path(source) == str(
        tree["games"] / "Abaron (1995).dosz",
    )


def test_the_product_can_go_to_another_folder(tree, tmp_path):
    source = str(tree["games"] / "Abaron (1995).zip")

    assert ExodosService().get_output_path(source, str(tmp_path)) == str(
        tmp_path / "Abaron (1995).dosz",
    )


# --- convert ---------------------------------------------------------------


def _convert(service, source, output, cancel_event=None):
    async def _drain():
        return [
            update async for update in service.convert(
                source, output, "exodos_repack", cancel_event=cancel_event,
            )
        ]

    return asyncio.run(_drain())


def _members(path):
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_convert_re_roots_the_game_and_writes_the_rewritten_conf(tree):
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()
    output = service.get_output_path(source)

    updates = _convert(service, source, output)

    assert _members(output) == {
        "ABARON.EXE": b"data:Abaron/ABARON.EXE",
        "CD/ABARON.CUE": b"data:Abaron/CD/ABARON.CUE",
        "dosbox.conf": (
            b"[autoexec]\r\n"
            b"mount c .\\\r\n"
            b"imgmount d .\\CD\\ABARON.CUE -t cdrom\r\n"
            b"c:\r\n"
            b"ABARON.EXE\r\n"
        ),
    }
    assert Path(source).read_bytes() == before
    assert updates[-1]["progress"] == 100
    assert not updates[-1].get("warning")


def test_a_conf_inside_the_game_folder_is_replaced_by_the_template(tree):
    service = ExodosService()
    source = _game_zip(tree, ["Abaron/A.EXE", "Abaron/dosbox.conf"])
    output = service.get_output_path(source)

    _convert(service, source, output)

    with zipfile.ZipFile(output) as zf:
        assert zf.namelist().count("dosbox.conf") == 1
        assert zf.read("dosbox.conf").startswith(b"[autoexec]")


def test_convert_warns_when_the_conf_still_points_into_the_pack(tree):
    (tree["dos"] / "Abaron" / "dosbox.conf").write_bytes(
        TEMPLATE + b"imgmount e .\\eXoDOS\\Other\\CD.CUE -t cdrom\r\n",
    )
    service = ExodosService()
    source = _game_zip(tree)

    updates = _convert(service, source, service.get_output_path(source))

    assert updates[-1]["warning"] is True
    assert "imgmount e .\\eXoDOS\\Other\\CD.CUE" in updates[-1]["message"]


def test_cancel_leaves_neither_product_nor_temp_file(tree):
    service = ExodosService()
    source = _game_zip(tree)
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(ConversionCancelled):
        _convert(service, source, service.get_output_path(source), cancel)

    assert sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]


def test_convert_refuses_a_zip_that_is_not_repackable(tree):
    service = ExodosService()
    source = _game_zip(tree, ["Loose.EXE"], name="Loose.zip")

    with pytest.raises(NotRepackable):
        _convert(service, source, service.get_output_path(source))


# --- verify ----------------------------------------------------------------


def _verify(service, path, cancel_event=None):
    return asyncio.run(service.verify(path, cancel_event=cancel_event))


def _product(tree):
    service = ExodosService()
    source = _game_zip(tree)
    output = service.get_output_path(source)
    _convert(service, source, output)
    return service, output


def test_a_finished_product_verifies_intact(tree):
    service, output = _product(tree)

    result = _verify(service, output)

    assert result["valid"] is True
    assert "Intact" in result["message"]


def test_a_member_with_a_bad_crc_fails_verify(tree, tmp_path):
    service = ExodosService()
    bad = tmp_path / "vol" / "bad.dosz"
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("dosbox.conf", b"[autoexec]\r\n")
        zf.writestr("GAME.EXE", b"g" * 5000)
    raw = bytearray(bad.read_bytes())
    raw[raw.index(b"g" * 100) + 10] ^= 0xFF
    bad.write_bytes(bytes(raw))

    result = _verify(service, str(bad))

    assert result["valid"] is False
    assert "GAME.EXE" in result["message"]


def test_a_product_without_a_root_conf_fails_verify(tree, tmp_path):
    service = ExodosService()
    path = _zip(tmp_path / "vol" / "noconf.dosz", ["GAME.EXE"])

    result = _verify(service, path)

    assert result["valid"] is False
    assert "dosbox.conf" in result["message"]


def test_a_product_with_an_unsafe_member_fails_verify(tree, tmp_path):
    service = ExodosService()
    path = _zip(tmp_path / "vol" / "evil.dosz", ["dosbox.conf", "../evil"])

    result = _verify(service, path)

    assert result["valid"] is False


def test_a_cancelled_verify_reaches_no_verdict(tree):
    service, output = _product(tree)
    cancel = asyncio.Event()
    cancel.set()

    result = _verify(service, output, cancel)

    assert result.get("cancelled") is True


# --- the job's own check ---------------------------------------------------


def test_dosbox_conf_is_the_first_entry(tree):
    service = ExodosService()
    source = _game_zip(tree)
    output = service.get_output_path(source)

    _convert(service, source, output)

    with zipfile.ZipFile(output) as zf:
        assert zf.namelist()[0] == "dosbox.conf"


def test_a_product_that_fails_its_check_is_never_kept(tree, monkeypatch):
    """Every repack checks its own output before reporting success."""
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()
    output = service.get_output_path(source)
    real_check = ExodosService._check_product

    def corrupt_then_check(self, path):
        with open(path, "r+b") as handle:
            handle.truncate(os.path.getsize(path) // 2)
        return real_check(self, path)

    monkeypatch.setattr(ExodosService, "_check_product", corrupt_then_check)

    with pytest.raises(RuntimeError, match="failed its own check"):
        _convert(service, source, output)

    assert sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]
    assert Path(source).read_bytes() == before


def test_repacking_twice_gives_the_same_bytes(tree, tmp_path):
    service = ExodosService()
    source = _game_zip(tree)
    first = service.get_output_path(source)
    second_dir = tree["volume"] / "again"
    second_dir.mkdir()
    second = service.get_output_path(source, str(second_dir))

    _convert(service, source, first)
    _convert(service, source, second)

    assert Path(first).read_bytes() == Path(second).read_bytes()
