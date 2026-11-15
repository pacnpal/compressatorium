"""Create an .m3u from selected discs (#299).

Service functions for title, order and name choice, and the two routes
(preview, create) against a temp Volume. The writer is the shared one from
the .m3u Referrer package.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models import PlaylistCreateRequest, PlaylistPreviewRequest
from app.routes import files as files_routes
from app.services import disc_playlist
from app.services.referrers import write_new_bytes
from app.services.tools import registry


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


def _discs(folder: Path, *names: str) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        (folder / n).write_bytes(b"x")
        out.append(str(folder / n))
    return out


@pytest.mark.parametrize(
    ("name", "title", "number"),
    [
        ("Final Quest (Disc 1).cue", "Final Quest", 1),
        ("Final Quest (Disc 2 of 3).chd", "Final Quest", 2),
        ("Final Quest Disc 3.iso", "Final Quest", 3),
        ("Final Quest - CD2.cue", "Final Quest", 2),
        ("Final Quest [CD 1].gdi", "Final Quest", 1),
        ("Final Quest (Disc 1) (USA).cue", "Final Quest (USA)", 1),
        ("Final Quest (USA) (Disc 1).cue", "Final Quest (USA)", 1),
        ("Final Quest, Disc 1.cue", "Final Quest", 1),
        ("Final Quest.cue", "Final Quest", None),
        ("Discworld (Disc 2).cue", "Discworld", 2),
    ],
)
def test_title_and_disc_number(name, title, number):
    assert disc_playlist.disc_title(name) == title
    assert disc_playlist.disc_number(name) == number


def test_preview_orders_by_disc_number_not_selection_order(vol: Path):
    paths = _discs(
        vol / "psx",
        "Game (Disc 10).cue", "Game (Disc 2).cue", "Game (Disc 1).cue",
    )
    plan = disc_playlist.plan_playlist(paths)
    assert plan.file_name == "Game.m3u"
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).cue", "Game (Disc 10).cue"]


def test_unnumbered_discs_fall_back_to_name_order(vol: Path):
    paths = _discs(vol, "Game B.cue", "Game A.cue")
    assert disc_playlist.plan_playlist(paths).entries == ["Game A.cue", "Game B.cue"]


def test_numbered_discs_come_before_unnumbered(vol: Path):
    paths = _discs(vol, "Game.cue", "Game (Disc 2).cue", "Game (Disc 1).cue")
    assert disc_playlist.plan_playlist(paths).entries == [
        "Game (Disc 1).cue", "Game (Disc 2).cue", "Game.cue",
    ]


def test_title_override_wins(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    assert disc_playlist.plan_playlist(paths, title="My Game").file_name == "My Game.m3u"


def test_existing_playlist_offers_next_free_name(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    (vol / "Game.m3u").write_text("keep me")
    (vol / "Game (2).m3u").write_text("keep me too")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.requested_name == "Game.m3u"
    assert plan.file_name == "Game (3).m3u"
    assert plan.name_taken is True


def test_disc_extensions_come_from_the_registry(vol: Path):
    assert {".cue", ".chd", ".iso", ".gdi"} <= set(registry.disc_extensions())
    paths = _discs(vol, "a.nsz", "b.nsz")
    with pytest.raises(disc_playlist.PlaylistError, match="disc"):
        disc_playlist.plan_playlist(paths)


def test_bin_tracks_are_not_disc_images(vol: Path):
    paths = _discs(vol, "Game (Disc 1).bin", "Game (Disc 2).bin")
    with pytest.raises(disc_playlist.PlaylistError):
        disc_playlist.plan_playlist(paths)


def test_needs_two_or_more(vol: Path):
    with pytest.raises(disc_playlist.PlaylistError, match="two"):
        disc_playlist.plan_playlist(_discs(vol, "Game (Disc 1).cue"))


def test_one_folder_only(vol: Path):
    paths = _discs(vol / "a", "Game (Disc 1).cue") + _discs(vol / "b", "Game (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="one folder"):
        disc_playlist.plan_playlist(paths)


def test_outside_volumes_refused(vol: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere")
    paths = _discs(outside, "Game (Disc 1).cue", "Game (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="volume"):
        disc_playlist.plan_playlist(paths)


def test_missing_file_and_archive_members_refused(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue")
    with pytest.raises(disc_playlist.PlaylistError):
        disc_playlist.plan_playlist([*paths, str(vol / "Game (Disc 2).cue")])
    with pytest.raises(disc_playlist.PlaylistError):
        disc_playlist.plan_playlist([*paths, str(vol / "x.zip") + "::Game (Disc 2).cue"])


def test_duplicate_selection_is_not_two_discs(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="two"):
        disc_playlist.plan_playlist(paths + paths)


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "x\nint", "CON\x00"])
def test_bad_titles_refused(vol: Path, bad):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError):
        disc_playlist.plan_playlist(paths, title=bad)


@pytest.mark.asyncio
async def test_preview_route_writes_nothing(vol: Path):
    paths = _discs(vol, "Game (Disc 2).cue", "Game (Disc 1).cue")
    before = sorted(os.listdir(vol))
    out = await files_routes.playlist_preview(PlaylistPreviewRequest(paths=paths))
    assert sorted(os.listdir(vol)) == before
    assert out["file_name"] == "Game.m3u"
    assert out["entries"] == ["Game (Disc 1).cue", "Game (Disc 2).cue"]
    assert out["name_taken"] is False
    assert out["directory"] == str(vol)


@pytest.mark.asyncio
async def test_preview_route_maps_errors_to_400(vol: Path):
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_preview(
            PlaylistPreviewRequest(paths=_discs(vol, "Game (Disc 1).cue")),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_route_writes_the_previewed_playlist(vol: Path):
    paths = _discs(vol, "Game (Disc 2).cue", "Game (Disc 1).cue")
    preview = await files_routes.playlist_preview(PlaylistPreviewRequest(paths=paths))
    out = await files_routes.playlist_create(
        PlaylistCreateRequest(paths=paths, file_name=preview["file_name"]),
    )
    written = (vol / "Game.m3u").read_bytes()
    assert written == b"Game (Disc 1).cue\nGame (Disc 2).cue\n"
    assert out["path"] == str(vol / "Game.m3u")
    assert out["entries"] == preview["entries"]


@pytest.mark.asyncio
async def test_create_never_overwrites_and_suggests_another_name(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    (vol / "Game.m3u").write_bytes(b"precious")
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_create(
            PlaylistCreateRequest(paths=paths, file_name="Game.m3u"),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["suggested_name"] == "Game (2).m3u"
    assert (vol / "Game.m3u").read_bytes() == b"precious"


@pytest.mark.asyncio
async def test_create_refuses_a_symlink_at_the_target(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    victim = vol / "victim.txt"
    victim.write_bytes(b"precious")
    os.symlink(victim, vol / "Game.m3u")
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_create(
            PlaylistCreateRequest(paths=paths, file_name="Game.m3u"),
        )
    assert exc.value.status_code == 409
    assert victim.read_bytes() == b"precious"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../Game.m3u", "a/Game.m3u", "Game.txt", "Game", ".m3u", ""])
async def test_create_rejects_unsafe_file_names(vol: Path, bad):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_create(PlaylistCreateRequest(paths=paths, file_name=bad))
    assert exc.value.status_code == 400
    assert sorted(p.name for p in vol.iterdir() if p.suffix == ".m3u") == []


def test_exclusive_write_has_one_winner_under_a_race(tmp_path: Path):
    target = str(tmp_path / "Game.m3u")
    results: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            write_new_bytes(target, f"writer {i}\n".encode())
            results.append("won")
        except FileExistsError:
            results.append("lost")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("won") == 1
    assert results.count("lost") == 7
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game.m3u"]  # no temp litter


def test_playlist_bytes_use_the_referrer_encoding():
    from app.services.referrers.m3u import playlist_bytes

    assert playlist_bytes(["A (Disc 1).cue", "é (Disc 2).cue"]) == (
        "A (Disc 1).cue\né (Disc 2).cue\n".encode()
    )


def test_a_name_starting_with_hash_stays_an_entry_not_a_comment(vol: Path):
    paths = _discs(vol, "#1 Game (Disc 1).cue", "#1 Game (Disc 2).cue")
    assert disc_playlist.plan_playlist(paths).entries == [
        "./#1 Game (Disc 1).cue", "./#1 Game (Disc 2).cue",
    ]


# --- fix round 1 ------------------------------------------------------------


@pytest.mark.parametrize("ext", [".rvz", ".gcz", ".wia", ".wbfs", ".cso", ".zso"])
def test_dolphin_and_maxcso_formats_are_disc_images(ext, vol: Path):
    assert ext in registry.disc_extensions()
    paths = _discs(vol, f"Game (Disc 1){ext}", f"Game (Disc 2){ext}")
    assert disc_playlist.plan_playlist(paths).file_name == "Game.m3u"


def _cue_with_bin(folder: Path, name: str) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.bin").write_bytes(b"x")
    (folder / f"{name}.cue").write_text(
        f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
    )
    return [str(folder / f"{name}.cue"), str(folder / f"{name}.bin")]


def test_ticked_bin_tracks_collapse_into_their_cue(vol: Path):
    paths = _cue_with_bin(vol, "Game (Disc 1)") + _cue_with_bin(vol, "Game (Disc 2)")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).cue"]
    assert any("2 track files" in w for w in plan.warnings)


def test_a_bin_no_selected_cue_names_is_still_refused(vol: Path):
    paths = _cue_with_bin(vol, "Game (Disc 1)")[:1] + _discs(vol, "Game (Disc 2).cue")
    paths += _discs(vol, "stray.bin")
    with pytest.raises(disc_playlist.PlaylistError, match="not a disc image"):
        disc_playlist.plan_playlist(paths)


def test_two_formats_of_one_disc_are_refused(vol: Path):
    paths = _discs(vol, "G (Disc 1).cue", "G (Disc 1).chd", "G (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="Disc 1.*twice"):
        disc_playlist.plan_playlist(paths)


def test_same_unnumbered_image_in_two_formats_is_refused(vol: Path):
    paths = _discs(vol, "game.cue", "game.chd")
    with pytest.raises(disc_playlist.PlaylistError, match="same disc"):
        disc_playlist.plan_playlist(paths)


# --- PR review: symlinks, entry encoding, name length -----------------------


def test_playlist_goes_in_the_folder_the_user_selected_from(vol: Path):
    real = vol / "real"
    _discs(real, "Game (Disc 1).cue", "Game (Disc 2).cue")
    os.symlink(real, vol / "link")
    paths = [str(vol / "link" / "Game (Disc 1).cue"), str(vol / "link" / "Game (Disc 2).cue")]
    plan = disc_playlist.plan_playlist(paths)
    assert plan.directory == str(vol / "link")
    out = disc_playlist.create_playlist(paths, plan.file_name)
    assert (real / "Game.m3u").read_bytes() == b"Game (Disc 1).cue\nGame (Disc 2).cue\n"
    assert out.directory == str(vol / "link")


def test_a_disc_that_is_a_symlink_into_another_folder_is_refused(vol: Path):
    _discs(vol / "b", "Game (Disc 1).cue", "Game (Disc 2).cue")
    (vol / "a").mkdir()
    os.symlink(vol / "b" / "Game (Disc 1).cue", vol / "a" / "Game (Disc 1).cue")
    paths = [str(vol / "a" / "Game (Disc 1).cue"), str(vol / "b" / "Game (Disc 2).cue")]
    with pytest.raises(disc_playlist.PlaylistError, match="one folder"):
        disc_playlist.plan_playlist(paths)


def test_an_entry_that_would_not_resolve_to_the_selected_file_is_refused(vol, monkeypatch):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    monkeypatch.setattr(disc_playlist, "same_file", lambda a, b: False)
    with pytest.raises(disc_playlist.PlaylistError, match="resolve"):
        disc_playlist.plan_playlist(paths)


def test_entries_starting_with_whitespace_are_protected_by_the_shared_encoder(vol: Path):
    paths = _discs(vol, " Game (Disc 1).cue", "Game (Disc 2).cue")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["./ Game (Disc 1).cue", "Game (Disc 2).cue"]
    disc_playlist.create_playlist(paths, plan.file_name)
    assert (vol / plan.file_name).read_text().splitlines() == plan.entries


def test_a_name_no_line_can_carry_is_refused(vol: Path):
    paths = _discs(vol, "\x1fGame (Disc 1).cue", "Game (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="can't be written"):
        disc_playlist.plan_playlist(paths)


def test_playlist_bytes_reads_the_rendered_file_back():
    from app.services.referrers.m3u import playlist_bytes

    assert playlist_bytes(["./ a.cue", "./#b.cue"]) == b"./ a.cue\n./#b.cue\n"
    with pytest.raises(ValueError):
        playlist_bytes([" a.cue"])  # a reader would trim it
    with pytest.raises(ValueError):
        playlist_bytes(["two\nlines.cue"])


_TITLE_MAX = 245  # 255 - ".m3u" - " (999)"


def test_a_longest_title_can_still_be_given_a_collision_suffix(vol: Path):
    title = "x" * _TITLE_MAX
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    (vol / f"{title}.m3u").write_text("keep")
    plan = disc_playlist.plan_playlist(paths, title=title)
    assert plan.file_name == f"{title} (2).m3u"
    disc_playlist.create_playlist(paths, plan.file_name)  # what the preview offered
    assert (vol / plan.file_name).exists()


def test_a_title_one_byte_over_the_limit_is_refused(vol: Path):
    paths = _discs(vol, "Game (Disc 1).cue", "Game (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="too long"):
        disc_playlist.plan_playlist(paths, title="x" * (_TITLE_MAX + 1))


# --- PR review: name errors are reported apart from selection errors --------


def test_an_invalid_derived_title_is_a_name_error_that_carries_the_title(vol: Path):
    long = "x" * 246  # the longest title a 255-byte file name can derive is 251
    paths = _discs(vol, f"{long}.cue", f"{long}b.cue")
    with pytest.raises(disc_playlist.PlaylistNameError) as exc:
        disc_playlist.plan_playlist(paths)
    assert exc.value.title == long
    assert "too long" in str(exc.value)


def test_a_selection_error_is_not_a_name_error(vol: Path):
    with pytest.raises(disc_playlist.PlaylistError) as exc:
        disc_playlist.plan_playlist(_discs(vol, "Game (Disc 1).cue"))
    assert not isinstance(exc.value, disc_playlist.PlaylistNameError)


@pytest.mark.asyncio
async def test_preview_route_reports_a_name_error_with_a_code_and_the_title(vol: Path):
    long = "x" * 246  # the longest title a 255-byte file name can derive is 251
    paths = _discs(vol, f"{long}.cue", f"{long}b.cue")
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_preview(PlaylistPreviewRequest(paths=paths))
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_name"
    assert exc.value.detail["title"] == long
    assert "too long" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_preview_route_keeps_selection_errors_as_plain_text(vol: Path):
    with pytest.raises(HTTPException) as exc:
        await files_routes.playlist_preview(
            PlaylistPreviewRequest(paths=_discs(vol, "Game (Disc 1).cue")),
        )
    assert isinstance(exc.value.detail, str)


def test_create_validates_only_the_requested_name_not_the_derived_default(vol: Path):
    long = "x" * 246  # a derived default title this long is refused, a given name is not
    paths = _discs(vol, f"{long}.iso", f"{long}b.iso")
    with pytest.raises(disc_playlist.PlaylistNameError):
        disc_playlist.plan_playlist(paths)
    assert disc_playlist.plan_playlist(paths, title="Short").file_name == "Short.m3u"
    disc_playlist.create_playlist(paths, "Short.m3u")
    assert (vol / "Short.m3u").read_bytes() == f"{long}.iso\n{long}b.iso\n".encode()


def test_sibling_symlinks_into_another_folder_are_refused(vol: Path):
    # The .m3u Referrer looks in the resolved source folder and its ancestors,
    # so a playlist beside symlinks would never follow the discs when they convert.
    _discs(vol / "b", "Game (Disc 1).cue", "Game (Disc 2).cue")
    (vol / "a").mkdir()
    paths = []
    for n in ("Game (Disc 1).cue", "Game (Disc 2).cue"):
        os.symlink(vol / "b" / n, vol / "a" / n)
        paths.append(str(vol / "a" / n))
    with pytest.raises(disc_playlist.PlaylistError, match="symlinks into another folder"):
        disc_playlist.plan_playlist(paths)
    assert not (vol / "a" / "Game.m3u").exists()


def test_a_symlinked_folder_that_resolves_to_the_source_folder_is_allowed(vol: Path):
    # The playlist folder resolves to the discs' own folder, so the Referrer finds it.
    real = vol / "psx" / ".discs"
    _discs(real, "Game (Disc 1).cue", "Game (Disc 2).cue")
    os.symlink(real, vol / "psx" / "view")
    paths = [str(vol / "psx" / "view" / n) for n in ("Game (Disc 1).cue", "Game (Disc 2).cue")]
    assert disc_playlist.plan_playlist(paths).directory == str(vol / "psx" / "view")


@pytest.mark.parametrize(
    "awkward",
    ["a\\b (Disc 1).cue", "#a (Disc 1).cue", "\ufeffa (Disc 1).cue",
     "\u3000a (Disc 1).cue", "\u00a0a (Disc 1).cue", " a (Disc 1).cue", "a (Disc 1) .cue", "\u00e9 (Disc 1).cue"],
)
def test_awkward_names_read_back_to_the_selected_files(vol: Path, awkward):
    from app.services.referrers.m3u import entries

    other = awkward.replace("(Disc 1)", "(Disc 2)")
    paths = _discs(vol, awkward, other)
    disc_playlist.create_playlist(paths, "Out.m3u")
    read = entries((vol / "Out.m3u").read_bytes())
    assert len(read) == 2
    selected = {os.path.realpath(p) for p in paths}
    assert {os.path.realpath(vol / e) for e in read} == selected


def test_a_name_with_a_pipe_is_refused_because_readers_split_it_as_a_label(vol: Path):
    paths = _discs(vol, "a|b (Disc 1).cue", "a|b (Disc 2).cue")
    with pytest.raises(disc_playlist.PlaylistError, match="can't be written"):
        disc_playlist.plan_playlist(paths)
