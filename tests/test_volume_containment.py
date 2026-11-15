"""Volume containment: a user-supplied path is confined before the filesystem
sees it.

``is_within_configured_volumes`` and ``containing_volume_root`` take paths
straight from API requests (Auto, Library roots, Rules). The path is first
normalised and checked lexically against the configured Volume roots, and
only a path that passes reaches ``Path.resolve`` (which follows symlinks on
disk); the resolved path is then checked again, which catches a symlink inside
a Volume that points out of it.
"""
from __future__ import annotations

import os

import pytest
from utils.path_utils import containing_volume_root, is_within_configured_volumes


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    (volume / "ps2").mkdir(parents=True)
    (volume / "ps2" / "game.iso").write_bytes(b"\0")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key").write_bytes(b"\0")
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    return volume


@pytest.mark.parametrize(
    "rel",
    [
        "../secret/key",
        "ps2/../../secret/key",
        "ps2/../../../../../../etc/passwd",
        "../volume-sibling/x.iso",
    ],
)
def test_traversal_out_of_the_volume_is_refused(volume, rel):
    path = os.path.join(str(volume), rel)

    assert is_within_configured_volumes(path) is False
    assert containing_volume_root(path) is None


def test_an_absolute_path_elsewhere_is_refused(volume, tmp_path):
    path = str(tmp_path / "secret" / "key")

    assert is_within_configured_volumes(path) is False
    assert containing_volume_root(path) is None


def test_a_symlink_inside_the_volume_pointing_out_is_refused(volume, tmp_path):
    link = volume / "ps2" / "escape"
    link.symlink_to(tmp_path / "secret")

    assert is_within_configured_volumes(str(link / "key")) is False
    assert containing_volume_root(str(link / "key")) is None


def test_a_path_inside_the_volume_is_accepted(volume):
    path = str(volume / "ps2" / "game.iso")

    assert is_within_configured_volumes(path) is True
    assert containing_volume_root(path) == volume.resolve()


def test_an_archive_member_is_judged_by_its_archive(volume):
    assert is_within_configured_volumes(f"{volume}/ps2/a.zip::disc.iso") is True
    assert is_within_configured_volumes(f"{volume}/../secret/a.zip::disc.iso") is False


def test_a_volume_configured_through_a_symlink_still_contains_its_files(tmp_path, monkeypatch):
    from config import settings

    real = tmp_path / "real"
    (real / "ps2").mkdir(parents=True)
    (real / "ps2" / "game.iso").write_bytes(b"\0")
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(settings, "chd_volumes", str(link))

    # Both spellings of the same file are inside the Volume.
    assert is_within_configured_volumes(str(link / "ps2" / "game.iso")) is True
    assert is_within_configured_volumes(str(real / "ps2" / "game.iso")) is True


@pytest.mark.parametrize("check", [is_within_configured_volumes, containing_volume_root])
def test_a_path_outside_every_volume_never_reaches_the_filesystem(
    volume, tmp_path, monkeypatch, check,
):
    from utils import path_utils

    touched: list[str] = []
    real = path_utils._resolve_path

    def spy(raw, **kwargs):
        touched.append(str(raw))
        return real(raw, **kwargs)

    monkeypatch.setattr(path_utils, "_resolve_path", spy)

    check(os.path.join(str(volume), "..", "secret", "key"))
    check(str(tmp_path / "secret" / "key"))

    # Confined lexically first: neither user path was resolved on disk.
    assert touched == []


def _symlink_then_dotdot(volume, tmp_path):
    """``/volume/link`` -> ``/outside/nested``; ``/volume/link/../secret.txt``
    folds lexically to ``/volume/secret.txt`` but the OS opens
    ``/outside/secret.txt``."""
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    secret = outside / "secret.txt"
    secret.write_bytes(b"keep me")
    (volume / "link").symlink_to(outside / "nested")
    return f"{volume}/link/../secret.txt", secret


def test_a_symlink_then_dotdot_escape_is_refused(volume, tmp_path):
    path, _secret = _symlink_then_dotdot(volume, tmp_path)

    assert is_within_configured_volumes(path) is False
    assert containing_volume_root(path) is None


@pytest.mark.parametrize(
    "rel", ["ps2/../ps2/game.iso", "./ps2/../ps2/game.iso", "ps2/..", ".."],
)
def test_any_dotdot_segment_is_refused_even_when_it_stays_inside(volume, rel):
    path = os.path.join(str(volume), rel)

    assert is_within_configured_volumes(path) is False
    assert containing_volume_root(path) is None


@pytest.mark.asyncio
async def test_delete_refuses_a_symlink_then_dotdot_escape(volume, tmp_path):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from starlette.datastructures import Headers

    from app.routes import files as files_routes

    path, secret = _symlink_then_dotdot(volume, tmp_path)
    request = SimpleNamespace(
        headers=Headers({"X-CHD-Action-Confirm": files_routes.CONFIRM_DELETE_FILE}),
    )

    with pytest.raises(HTTPException) as exc:
        await files_routes.delete_file(request, path=path, recursive=False)

    assert exc.value.status_code == 403
    assert secret.exists()
