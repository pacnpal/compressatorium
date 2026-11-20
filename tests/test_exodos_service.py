"""services.exodos.ExodosService: gating, zip inspection, confinement, output.

Synthetic fixtures only: a tiny eXoDOS-shaped tree under a temp Volume.
"""
from __future__ import annotations

import asyncio
import errno
import os
import threading
import time
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


# --- finding the template tree (#513) ---------------------------------------


def test_the_override_is_checked_first(tree, monkeypatch):
    override = tree["volume"] / "mine" / "!dos"
    (override / "Abaron").mkdir(parents=True)
    (override / "Abaron" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", str(override))

    plan = ExodosService().inspect(_game_zip(tree))

    assert plan.template == os.path.realpath(override / "Abaron" / "dosbox.conf")


@pytest.mark.parametrize("where", ["unset", "outside", "missing"])
def test_without_a_usable_override_the_tree_beside_the_zip_is_found(
    tree, tmp_path, monkeypatch, where,
):
    outside = tmp_path / "elsewhere" / "!dos"
    (outside / "Abaron").mkdir(parents=True)
    (outside / "Abaron" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    value = {"unset": None, "outside": str(outside), "missing": str(tree["volume"] / "nope")}
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", value[where])

    plan = ExodosService().inspect(_game_zip(tree))

    assert plan.template == os.path.realpath(tree["dos"] / "Abaron" / "dosbox.conf")


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


def test_inspect_refuses_a_folder_with_no_template_and_no_dos_program(tree):
    path = _game_zip(tree, ["Photos/IMG_0001.JPG"], name="Photos.zip")

    with pytest.raises(NotRepackable, match="no DOS program"):
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


def test_inspect_needs_no_setting(tree, monkeypatch):
    path = _game_zip(tree)
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", None)

    assert ExodosService().inspect(path).template == os.path.realpath(
        tree["dos"] / "Abaron" / "dosbox.conf",
    )


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


def test_a_product_with_neither_a_root_conf_nor_a_program_fails_verify(tree, tmp_path):
    service = ExodosService()
    path = _zip(tmp_path / "vol" / "noconf.dosz", ["README.TXT"])

    result = _verify(service, path)

    assert result["valid"] is False
    assert "dosbox.conf" in result["message"]


def test_a_product_with_no_conf_but_a_dos_program_verifies(tree, tmp_path):
    path = _zip(tmp_path / "vol" / "noconf.dosz", ["BIN/GAME.EXE"])

    assert _verify(ExodosService(), path)["valid"] is True


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

    def corrupt_then_check(self, path, *args, **kwargs):
        with open(path, "r+b") as handle:
            handle.truncate(os.path.getsize(path) // 2)
        return real_check(self, path, *args, **kwargs)

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


# --- review round 1 --------------------------------------------------------


def test_convert_warns_about_folder_mounts_on_other_drives(tree):
    (tree["dos"] / "Abaron" / "dosbox.conf").write_bytes(
        TEMPLATE + b"mount e .\\eXoDOS\\Abaron\\CD -t cdrom\r\n",
    )
    service = ExodosService()
    source = _game_zip(tree)

    updates = _convert(service, source, service.get_output_path(source))

    assert updates[-1]["warning"] is True
    assert "mount e .\\CD -t cdrom" in updates[-1]["message"]


def test_a_template_added_later_is_seen_without_a_restart(tree):
    service = ExodosService()
    path = _game_zip(tree, ["Zork/ZORK.DAT"], name="Zork.zip")

    assert service.is_repackable(path) is False
    (tree["dos"] / "Zork").mkdir()
    (tree["dos"] / "Zork" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")

    assert service.is_repackable(path) is True


def test_a_member_name_padded_with_spaces_is_refused(tree):
    path = _game_zip(tree, ["Abaron/A.EXE", " Abaron/B.EXE"])

    with pytest.raises(NotRepackable, match="spaces"):
        ExodosService().inspect(path)


def test_inspect_applies_the_member_size_limit(tree, monkeypatch):
    monkeypatch.setattr(exodos_module.settings, "archive_max_member_size", 5)

    with pytest.raises(NotRepackable, match="max size"):
        ExodosService().inspect(_game_zip(tree))


def test_inspect_applies_the_total_size_limit(tree, monkeypatch):
    monkeypatch.setattr(exodos_module.settings, "archive_max_total_size", 10)

    with pytest.raises(NotRepackable, match="max total size"):
        ExodosService().inspect(_game_zip(tree))


def test_the_warning_reads_as_sentences(tree):
    (tree["dos"] / "Abaron" / "dosbox.conf").write_bytes(
        TEMPLATE + b"imgmount e .\\eXoDOS\\Other\\CD.CUE -t cdrom\r\n",
    )
    service = ExodosService()
    source = _game_zip(tree)

    message = _convert(service, source, service.get_output_path(source))[-1]["message"]

    assert message.startswith("Repacked with its dosbox.conf from ")
    assert ", but it may not start. The conf still points" in message
    assert message.endswith(".")


def test_a_cancel_during_the_check_keeps_nothing(tree, monkeypatch):
    """The check re-reads the whole product, so it must honour Cancel too."""
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()
    cancel = asyncio.Event()
    real_check = ExodosService._own_check

    def cancel_then_check(self, path, is_cancelled, **kwargs):
        cancel.set()
        return real_check(self, path, is_cancelled, **kwargs)

    monkeypatch.setattr(ExodosService, "_own_check", cancel_then_check)

    with pytest.raises(ConversionCancelled):
        _convert(service, source, service.get_output_path(source), cancel)

    assert sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]
    assert Path(source).read_bytes() == before


# --- a repack whose I/O stops answering (PR #324 review) --------------------


@pytest.fixture
def hung_write(monkeypatch):
    """The real rewrite, stuck in its first write the way a dead mount sticks.

    ``release`` lets the blocked "syscall" return; ``finished`` is set once
    the worker has run to its end, whatever that end was.
    """
    real = exodos_module.rewrite_zip
    release, finished = threading.Event(), threading.Event()

    def hung(src, dst, *, on_progress, **kwargs):
        def stuck(done, total):
            release.wait(30)
            on_progress(done, total)

        try:
            return real(src, dst, on_progress=stuck, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(exodos_module, "rewrite_zip", hung)
    yield {"release": release, "finished": finished}
    release.set()


def _no_product_and_no_temp(tree):
    return sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]


def test_cancel_returns_at_once_and_the_late_worker_publishes_nothing(tree, hung_write):
    service = ExodosService()
    source = _game_zip(tree)
    cancel = asyncio.Event()

    async def run():
        asyncio.get_running_loop().call_later(0.3, cancel.set)
        return [u async for u in service.convert(
            source, service.get_output_path(source), "exodos_repack", cancel_event=cancel,
        )]

    started = time.monotonic()
    with pytest.raises(ConversionCancelled):
        asyncio.run(run())
    assert time.monotonic() - started < 3

    # The blocked write finally returns: the worker sees it was abandoned,
    # removes its hidden temp file and places nothing.
    hung_write["release"].set()
    assert hung_write["finished"].wait(10)
    assert _no_product_and_no_temp(tree)


def test_a_stalled_repack_fails_and_the_late_worker_publishes_nothing(
    tree, hung_write, monkeypatch,
):
    from services import subprocess_runner

    monkeypatch.setattr(exodos_module.settings, "progress_timeout", 1)
    monkeypatch.setattr(subprocess_runner, "_MIN_STALL_TIMEOUT", 1)
    service = ExodosService()
    source = _game_zip(tree)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="stopped responding"):
        _convert(service, source, service.get_output_path(source))
    assert time.monotonic() - started < 5

    hung_write["release"].set()
    assert hung_write["finished"].wait(10)
    assert _no_product_and_no_temp(tree)


# --- the template read is confined (PR #324 security review) ---------------


def test_a_template_swapped_for_a_symlink_after_inspect_is_refused(tree, tmp_path, monkeypatch):
    """Codex's repro: swap dosbox.conf for a link between inspect and the read."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"API_TOKEN=hunter2")
    template = tree["dos"] / "Abaron" / "dosbox.conf"
    real_inspect = ExodosService.inspect

    def inspect_then_swap(self, path):
        plan = real_inspect(self, path)
        template.unlink()
        template.symlink_to(secret)
        return plan

    monkeypatch.setattr(ExodosService, "inspect", inspect_then_swap)
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()

    with pytest.raises(NotRepackable):
        _convert(service, source, service.get_output_path(source))

    assert sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]
    assert Path(source).read_bytes() == before


def test_a_symlinked_game_folder_inside_the_tree_is_refused(tree):
    (tree["dos"] / "Other").mkdir()
    (tree["dos"] / "Other" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    (tree["dos"] / "Zork").symlink_to(tree["dos"] / "Other")
    path = _game_zip(tree, ["Zork/ZORK.EXE"], name="Zork.zip")

    with pytest.raises(NotRepackable, match="link"):
        ExodosService().inspect(path)


def test_a_game_mounted_from_a_subfolder_as_c_gets_a_warning(tree):
    """Codex's layout: C: is a subfolder, so `c:` then GAME.EXE finds nothing."""
    (tree["dos"] / "Abaron" / "dosbox.conf").write_bytes(
        b"[autoexec]\r\nmount c .\\eXoDOS\\Abaron\\HD\r\nc:\r\nGAME.EXE\r\n",
    )
    service = ExodosService()
    source = _game_zip(tree, ["Abaron/HD/GAME.EXE"])

    updates = _convert(service, source, service.get_output_path(source))

    assert updates[-1].get("warning") is True
    assert "mount c .\\HD" in updates[-1]["message"]


# --- verify edge cases (PR #324 CodeRabbit review) --------------------------


def _verify_events(service, path, cancel_event=None):
    async def _drain():
        return [e async for e in service.verify_stream(path, cancel_event=cancel_event)]

    return asyncio.run(_drain())


def _raw_dosz(path, members, method=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", method) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def test_shared_member_rules_refuse_a_padded_name():
    from services.archive import archive_service

    with pytest.raises(ValueError, match="spaces"):
        archive_service.member_segments(" Abaron/x")


def test_verify_refuses_a_padded_member_name(tree):
    path = _raw_dosz(tree["volume"] / "pad.dosz", {"dosbox.conf": b"c", " GAME.EXE": b"g"})

    result = asyncio.run(ExodosService().verify(path))

    assert result["valid"] is False


def test_a_cancel_raised_inside_the_check_is_a_cancelled_verdict(tree, monkeypatch):
    path = _raw_dosz(tree["volume"] / "ok.dosz", {"dosbox.conf": b"c", "G.EXE": b"g"})

    def cancelled(self, *_args):
        raise ConversionCancelled("Conversion cancelled")

    monkeypatch.setattr(ExodosService, "_check_product", cancelled)

    events = _verify_events(ExodosService(), path)

    assert events[-1].get("cancelled") is True


def test_a_limit_refusal_is_not_checkable_rather_than_failed(tree, monkeypatch):
    path = _raw_dosz(tree["volume"] / "ok.dosz", {"dosbox.conf": b"c", "G.EXE": b"g"})
    monkeypatch.setattr(exodos_module.settings, "archive_max_entries", 1)

    events = _verify_events(ExodosService(), path)

    assert events[-1].get("uncheckable") is True


def test_a_damaged_product_still_fails(tree):
    path = tree["volume"] / "bad.dosz"
    _raw_dosz(path, {"dosbox.conf": b"c", "G.EXE": b"g" * 5000})
    raw = bytearray(path.read_bytes())
    raw[raw.index(b"g" * 100) + 10] ^= 0xFF
    path.write_bytes(bytes(raw))

    events = _verify_events(ExodosService(), str(path))

    assert events[-1]["valid"] is False
    assert not events[-1].get("uncheckable")


def _patch_member_header(path, name: bytes, *, flag=None, method=None):
    """Rewrite a member's general-purpose flag or method in both headers."""
    raw = bytearray(Path(path).read_bytes())
    for sig, flag_at, method_at in ((b"PK\x03\x04", 6, 8), (b"PK\x01\x02", 8, 10)):
        start = 0
        while (at := raw.find(sig, start)) >= 0:
            name_at = at + (30 if sig == b"PK\x03\x04" else 46)
            if raw[name_at:name_at + len(name)] == name:
                if flag is not None:
                    raw[at + flag_at] |= flag
                if method is not None:
                    raw[at + method_at:at + method_at + 2] = method.to_bytes(2, "little")
            start = at + 4
    Path(path).write_bytes(bytes(raw))


@pytest.mark.parametrize(("flag", "method"), [(0x1, None), (None, 99)], ids=["encrypted", "unknown-method"])
def test_an_unreadable_member_fails_verify_instead_of_raising(tree, flag, method):
    path = _raw_dosz(tree["volume"] / "odd.dosz", {"dosbox.conf": b"c", "G.EXE": b"g" * 100})
    _patch_member_header(path, b"G.EXE", flag=flag, method=method)

    result = asyncio.run(ExodosService().verify(path))

    assert result["valid"] is False
    assert "G.EXE" in result["message"]


# --- the reads before the rewrite are bounded too (PR #324 Codex review) ----


@pytest.fixture
def blocked_step(monkeypatch):
    """Make one pre-rewrite read block, as a hung Volume would."""
    release = threading.Event()

    def block(name):
        real = getattr(ExodosService, name)

        def stuck(self, *args):
            release.wait(30)
            return real(self, *args)

        monkeypatch.setattr(ExodosService, name, stuck)

    yield block
    release.set()


@pytest.mark.parametrize("step", ["inspect", "_read_template"])
def test_cancel_returns_while_a_pre_rewrite_read_is_blocked(tree, blocked_step, step):
    blocked_step(step)
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()
    cancel = asyncio.Event()

    async def run():
        asyncio.get_running_loop().call_later(0.3, cancel.set)
        return [u async for u in service.convert(
            source, service.get_output_path(source), "exodos_repack", cancel_event=cancel,
        )]

    started = time.monotonic()
    with pytest.raises(ConversionCancelled):
        asyncio.run(run())

    assert time.monotonic() - started < 3
    assert Path(source).read_bytes() == before


@pytest.mark.parametrize("step", ["inspect", "_read_template"])
def test_a_stalled_pre_rewrite_read_fails_the_job_and_keeps_the_source(
    tree, blocked_step, monkeypatch, step,
):
    from services import subprocess_runner

    monkeypatch.setattr(exodos_module.settings, "progress_timeout", 1)
    monkeypatch.setattr(subprocess_runner, "_MIN_STALL_TIMEOUT", 1)
    blocked_step(step)
    service = ExodosService()
    source = _game_zip(tree)
    before = Path(source).read_bytes()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="stopped responding"):
        _convert(service, source, service.get_output_path(source))

    assert time.monotonic() - started < 5
    assert Path(source).read_bytes() == before
    assert sorted(os.listdir(tree["games"])) == ["!dos", "Abaron (1995).zip"]


# --- non-eXoDOS misses are cached too (#331) ---------------------------------


def _count_inspections(monkeypatch):
    """Count the zip reads a probe makes (its first step)."""
    calls = []
    real = ExodosService._read_zip

    def counted(self, path):
        calls.append(path)
        return real(self, path)

    monkeypatch.setattr(ExodosService, "_read_zip", counted)
    return calls


def test_a_miss_is_cached(tree, monkeypatch):
    calls = _count_inspections(monkeypatch)
    service = ExodosService()
    path = _game_zip(tree, ["README.TXT"], name="NotAGame.zip")

    assert service.is_repackable(path) is False
    assert service.is_repackable(path) is False

    assert calls == [path]


def test_a_template_added_to_an_existing_game_folder_ends_a_cached_miss(tree, monkeypatch):
    calls = _count_inspections(monkeypatch)
    service = ExodosService()
    (tree["dos"] / "Zork").mkdir()
    path = _game_zip(tree, ["Zork/ZORK.DAT"], name="Zork.zip")
    assert service.is_repackable(path) is False

    conf = tree["dos"] / "Zork" / "dosbox.conf"
    conf.write_bytes(b"[autoexec]\r\n")
    folder = tree["dos"] / "Zork"
    later = os.stat(folder).st_mtime + 5
    os.utime(folder, (later, later))

    assert service.is_repackable(path) is True
    assert len(calls) == 2


def test_a_template_fixed_in_place_ends_a_cached_miss(tree):
    """An oversized template is a miss; trimming the same file (no folder
    change) must be seen."""
    service = ExodosService()
    template = tree["dos"] / "Abaron" / "dosbox.conf"
    template.write_bytes(b"[autoexec]\r\n" + b"x" * (exodos_module._MAX_TEMPLATE_BYTES + 1))
    folder = os.stat(template.parent)
    folder_times = (folder.st_atime_ns, folder.st_mtime_ns)
    path = _game_zip(tree)
    assert service.is_repackable(path) is False

    with open(template, "r+b") as handle:  # in place: same inode, same folder
        handle.truncate(len(TEMPLATE))
        handle.seek(0)
        handle.write(TEMPLATE)
    os.utime(template.parent, ns=folder_times)
    assert os.stat(template.parent).st_mtime_ns == folder_times[1]

    assert service.is_repackable(path) is True


def test_a_template_read_that_fails_for_a_moment_is_not_remembered(tree, monkeypatch):
    """A permission or I/O error is not an answer: fixing it changes no stamp,
    so it must not be cached as a miss."""
    service = ExodosService()
    path = _game_zip(tree)
    real = exodos_module.read_beneath
    calls = []

    def flaky(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(exodos_module, "read_beneath", flaky)

    assert service.is_repackable(path) is False
    assert service.is_repackable(path) is True


def test_a_template_made_while_the_probe_runs_is_not_cached_as_missing(tree, monkeypatch):
    """The template appears after the read found none but before the answer
    is stored: the miss must not be cached under the tree's new stamp."""
    service = ExodosService()
    template = tree["dos"] / "Abaron" / "dosbox.conf"
    template.unlink()
    path = _game_zip(tree, ["Abaron/CD/ABARON.CUE"])
    real = ExodosService._read_template

    def racing(self, root, top):
        try:
            return real(self, root, top)
        finally:
            if not template.exists():
                template.write_bytes(TEMPLATE)

    monkeypatch.setattr(ExodosService, "_read_template", racing)

    assert service.is_repackable(path) is False
    assert service.is_repackable(path) is True


def test_a_zip_that_cannot_be_read_for_a_moment_is_not_remembered(tree, monkeypatch):
    """EACCES on the game zip is not a verdict: restoring access changes
    neither its size nor its mtime, so the miss must not be cached."""
    service = ExodosService()
    path = _game_zip(tree)
    real = zipfile.ZipFile
    calls = []

    def flaky(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return real(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", flaky)

    assert service.is_repackable(path) is False
    assert service.is_repackable(path) is True


# --- no setting, and no template (#513) -------------------------------------


@pytest.fixture
def bare(tree, monkeypatch):
    """The eXoDOS layout with EXODOS_DOS_DIR unset, as a fresh install has it."""
    monkeypatch.setattr(exodos_module.settings, "exodos_dos_dir", None)
    return tree


def test_the_tree_is_found_in_a_parent_folder(bare):
    sub = bare["games"] / "Adventure"
    sub.mkdir()
    path = _zip(sub / "Abaron.zip", ["Abaron/ABARON.EXE"])

    plan = ExodosService().inspect(path)

    assert plan.template == os.path.realpath(bare["dos"] / "Abaron" / "dosbox.conf")


def test_the_nearest_tree_holding_the_game_wins(bare):
    sub = bare["games"] / "Adventure"
    (sub / "!dos" / "Abaron").mkdir(parents=True)
    (sub / "!dos" / "Abaron" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    path = _zip(sub / "Abaron.zip", ["Abaron/ABARON.EXE"])

    plan = ExodosService().inspect(path)

    assert plan.template == os.path.realpath(sub / "!dos" / "Abaron" / "dosbox.conf")


def test_a_nearer_tree_without_the_game_is_passed_over(bare):
    sub = bare["games"] / "Adventure"
    (sub / "!dos" / "Other").mkdir(parents=True)
    path = _zip(sub / "Abaron.zip", ["Abaron/ABARON.EXE"])

    plan = ExodosService().inspect(path)

    assert plan.template == os.path.realpath(bare["dos"] / "Abaron" / "dosbox.conf")


def test_the_walk_stops_at_the_volume(bare, monkeypatch):
    """A !dos folder above the Volume is never read."""
    games = bare["games"] / "inner"
    games.mkdir()
    monkeypatch.setattr(exodos_module.settings, "chd_volumes", str(games))
    path = _zip(games / "Abaron.zip", ["Abaron/ABARON.EXE"])

    plan = ExodosService().inspect(path)

    assert plan.template is None


def test_a_dos_folder_linked_out_of_the_volume_is_not_used(bare, tmp_path):
    outside = tmp_path / "elsewhere" / "!dos"
    (outside / "Abaron").mkdir(parents=True)
    (outside / "Abaron" / "dosbox.conf").write_bytes(b"[autoexec]\r\n")
    sub = bare["games"] / "Adventure"
    sub.mkdir()
    (sub / "!dos").symlink_to(outside)
    path = _zip(sub / "Abaron.zip", ["Abaron/ABARON.EXE"])

    plan = ExodosService().inspect(path)

    assert plan.template == os.path.realpath(bare["dos"] / "Abaron" / "dosbox.conf")


def test_a_game_with_no_template_is_repacked_without_a_conf(bare):
    service = ExodosService()
    source = _game_zip(bare, ["Zork/", "Zork/ZORK.EXE", "Zork/DATA/ZORK.DAT"], name="Zork.zip")
    before = Path(source).read_bytes()
    output = service.get_output_path(source)

    assert service.is_repackable(source) is True
    updates = _convert(service, source, output)

    assert _members(output) == {
        "ZORK.EXE": b"data:Zork/ZORK.EXE",
        "DATA/ZORK.DAT": b"data:Zork/DATA/ZORK.DAT",
    }
    assert Path(source).read_bytes() == before
    assert updates[-1]["progress"] == 100
    assert updates[-1]["warning"] is True
    assert "no dosbox.conf" in updates[-1]["message"]
    assert "start menu" in updates[-1]["message"]
    assert _verify(service, output)["valid"] is True


def test_a_game_with_its_template_says_where_its_conf_came_from(bare):
    service = ExodosService()
    source = _game_zip(bare)

    updates = _convert(service, source, service.get_output_path(source))

    assert not updates[-1].get("warning")
    assert os.path.join("!dos", "Abaron", "dosbox.conf") in updates[-1]["message"]


def test_a_template_that_cannot_be_read_does_not_fall_back_to_no_conf(bare, monkeypatch):
    """Fail closed: a template that is there but unreadable just now is no
    licence to repack without it."""
    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(exodos_module, "read_beneath", denied)

    with pytest.raises(NotRepackable, match="could not be read"):
        ExodosService().inspect(_game_zip(bare))


def test_a_game_with_no_template_leaves_out_its_own_untouched_conf(bare):
    """Codex PR #517: the zip's own ``<Game>/dosbox.conf`` was never
    rewritten, so it would land at the root and DOSBox Pure would load it
    (``dosbox_pure_conf: inside``) instead of the start menu promised."""
    service = ExodosService()
    source = _game_zip(
        bare, ["Zork/", "Zork/ZORK.EXE", "Zork/dosbox.conf", "Zork/SUB/dosbox.conf"],
        name="Zork.zip",
    )
    output = service.get_output_path(source)

    updates = _convert(service, source, output)

    assert set(_members(output)) == {"ZORK.EXE", "SUB/dosbox.conf"}
    assert "no dosbox.conf" in updates[-1]["message"]
    assert "left out" in updates[-1]["message"]
    assert _verify(service, output)["valid"] is True
