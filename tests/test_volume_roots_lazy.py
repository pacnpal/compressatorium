"""The containment gate resolves only the Volume roots it needs (Codex 4576916119).

``_confined_real_path`` used to ``realpath()`` every configured Volume before it
matched a path, so one stalled mount blocked containment checks for files on
a healthy Volume (``GET /api/files`` among them). Now a path is matched against
the configured spellings with no filesystem call; only the matching root is
resolved, and the resolved spellings of the others are consulted only when no
configured spelling matches. Nothing is cached across calls: mounts change.
"""
from __future__ import annotations

import os

import pytest
from utils import path_utils


@pytest.fixture(name="volumes")
def _volumes(tmp_path, monkeypatch):
    from config import settings

    healthy = tmp_path / "healthy"
    stalled = tmp_path / "stalled"
    (healthy / "games").mkdir(parents=True)
    stalled.mkdir()
    (healthy / "games" / "game.iso").write_bytes(b"x")
    monkeypatch.setattr(settings, "chd_volumes", f"{healthy},{stalled}")
    assert [os.path.normpath(v) for v in settings.volumes] == [str(healthy), str(stalled)]

    touched: list[str] = []
    real_realpath = os.path.realpath

    def guarded_realpath(path, *args, **kwargs):
        if os.fspath(path).startswith(str(stalled)):
            touched.append(os.fspath(path))
            raise OSError("this mount is not answering")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", guarded_realpath)
    return {"healthy": healthy, "stalled": stalled, "touched": touched}


@pytest.mark.parametrize(
    "check",
    [
        lambda p: path_utils.is_within_configured_volumes(p),
        lambda p: path_utils.ensure_path_within_volumes(p) is not None,
        lambda p: path_utils.containing_volume_root(p) is not None,
    ],
    ids=["is_within", "ensure", "containing_root"],
)
def test_a_path_on_a_healthy_volume_never_touches_another(volumes, check):
    path = str(volumes["healthy"] / "games" / "game.iso")

    assert check(path)
    assert volumes["touched"] == []


def test_a_path_on_a_later_volume_never_touches_an_earlier_stalled_one(volumes, monkeypatch):
    from config import settings

    monkeypatch.setattr(
        settings, "chd_volumes", f"{volumes['stalled']},{volumes['healthy']}",
    )

    assert path_utils.is_within_configured_volumes(
        str(volumes["healthy"] / "games" / "game.iso"),
    )
    assert volumes["touched"] == []


def test_the_healthy_root_itself_never_touches_another(volumes):
    assert path_utils.is_within_configured_volumes(str(volumes["healthy"]))
    assert volumes["touched"] == []


def test_a_path_outside_every_volume_is_still_refused(volumes, tmp_path):
    (tmp_path / "elsewhere").mkdir()

    assert not path_utils.is_within_configured_volumes(str(tmp_path / "elsewhere"))


def test_a_volume_configured_through_a_symlink_still_matches_its_real_spelling(
    tmp_path, monkeypatch,
):
    from config import settings

    real = tmp_path / "real"
    (real / "games").mkdir(parents=True)
    (real / "games" / "game.iso").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(settings, "chd_volumes", str(link))

    assert path_utils.is_within_configured_volumes(str(link / "games" / "game.iso"))
    # No configured spelling matches; the resolved one does.
    assert path_utils.is_within_configured_volumes(str(real / "games" / "game.iso"))
