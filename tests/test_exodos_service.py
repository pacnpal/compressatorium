"""services.exodos.ExodosService: gating, zip inspection, confinement, output.

Synthetic fixtures only: a tiny eXoDOS-shaped tree under a temp Volume.
"""
from __future__ import annotations

import os
import zipfile

import pytest
from services import exodos as exodos_module
from services.exodos import ExodosService, NotRepackable

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
