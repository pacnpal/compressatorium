"""Platform detection: an Image's Platform worked out from its own bytes.

Fixtures are tiny synthetic headers (a few KiB, or a sparse file where a real
layout puts the header deep in the image), never real disc images.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import Detection, detect_platform

from .test_disc_id import _make_ipbin, _make_iso, _make_param_sfo
from .test_xiso_service import build_image


@pytest.fixture(autouse=True)
def _fresh_cache():
    platform_detect.clear_cache()
    yield
    platform_detect.clear_cache()


@pytest.fixture(autouse=True)
def _volume(tmp_path, monkeypatch):
    """``tmp_path`` is the configured Volume, so the folder walk reaches every
    folder a test builds under it (and nothing above it)."""
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _gamecube(path: Path) -> str:
    head = bytearray(0x440)
    head[0:6] = b"GALE01"
    struct.pack_into(">I", head, 0x1C, 0xC2339F3D)
    return _write(path, bytes(head))


def _wii(path: Path) -> str:
    head = bytearray(0x440)
    head[0:6] = b"RSPE01"
    struct.pack_into(">I", head, 0x18, 0x5D1C9EA3)
    return _write(path, bytes(head))


def _ps2(path: Path) -> str:
    cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\nVER = 1.00\n"
    return _write(path, _make_iso({"SYSTEM.CNF": cnf}))


def _ps1(path: Path) -> str:
    cnf = b"BOOT = cdrom:\\SCUS_941.63;1\nTCB = 4\n"
    return _write(path, _make_iso({"SYSTEM.CNF": cnf}))


def _psp(path: Path) -> str:
    sfo = _make_param_sfo({"DISC_ID": "ULUS10041", "TITLE": "Test"})
    return _write(path, _make_iso({"PSP_GAME/PARAM.SFO": sfo}))


def _sector0(path: Path, magic: bytes, *, raw: bool = False) -> str:
    """A disc whose first data sector starts with ``magic``.

    ``raw`` writes it the way a 2352-byte BIN track holds it: 16 bytes of sync
    and header, then the user data.
    """
    sector = bytearray(2352 if raw else 2048)
    start = 16 if raw else 0
    sector[start:start + len(magic)] = magic
    return _write(path, bytes(sector))


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("builder", "name", "expected"),
    [
        (_gamecube, "game.iso", "ngc"),
        (_wii, "game.iso", "wii"),
        (_ps2, "game.iso", "ps2"),
        (_ps1, "game.iso", "psx"),
        (_psp, "game.iso", "psp"),
        (lambda p: _sector0(p, b"SEGA SEGASATURN "), "game.iso", "saturn"),
        (lambda p: _sector0(p, b"SEGA SEGASATURN ", raw=True), "game.bin", "saturn"),
        (lambda p: _sector0(p, b"SEGADISCSYSTEM  "), "game.iso", "segacd"),
        (lambda p: _sector0(p, b"SEGADISCSYSTEM  ", raw=True), "game.bin", "segacd"),
        (lambda p: _write(p, _make_ipbin()), "game.iso", "dc"),
    ],
    ids=[
        "gamecube", "wii", "ps2", "ps1", "psp", "saturn-iso", "saturn-bin",
        "segacd-iso", "segacd-bin", "dreamcast-ipbin",
    ],
)
def test_header_names_the_platform(tmp_path, builder, name, expected):
    path = builder(tmp_path / "unsorted" / name)

    result = detect_platform(path)

    assert result == Detection(
        platform=expected, how="header", header=expected, folder=None,
        reason=result.reason,
    )


def test_xbox_360_header_from_its_layout(tmp_path):
    path = tmp_path / "stuff" / "halo3.iso"
    path.parent.mkdir()
    build_image(path, layout="XGD2", exe="default.xex")

    assert detect_platform(str(path)).platform == "xbox360"


def test_trimmed_xbox_image_named_by_its_boot_executable(tmp_path):
    path = tmp_path / "stuff" / "halo.iso"
    path.parent.mkdir()
    build_image(path, layout="XISO", exe="default.xbe")

    assert detect_platform(str(path)).platform == "xbox"


def test_dreamcast_gdi_reads_its_first_track(tmp_path):
    folder = tmp_path / "stuff"
    track = bytearray(2352)
    track[16:16 + 0x200] = _make_ipbin()
    _write(folder / "track01.bin", bytes(track))
    gdi = _write(folder / "game.gdi", b"1\n1 0 4 2352 track01.bin 0\n")

    assert detect_platform(gdi).platform == "dc"


# --------------------------------------------------------------------------- #
# Folder names
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        # Slugs the tools already declare.
        ("ps2", "ps2"), ("psp", "psp"), ("ngc", "ngc"), ("wii", "wii"),
        ("xbox", "xbox"), ("dc", "dc"), ("saturn", "saturn"), ("segacd", "segacd"),
        # ES-DE / Batocera names for the same consoles.
        ("psx", "psx"), ("ps1", "psx"), ("gc", "ngc"), ("gamecube", "ngc"), ("dreamcast", "dc"),
        ("megacd", "segacd"), ("PS2", "ps2"),
    ],
)
def test_folder_name_names_the_platform_when_there_is_no_header(
    tmp_path, folder, expected,
):
    path = _write(tmp_path / "roms" / folder / "Game (USA)" / "game.iso", b"\0" * 64)

    result = detect_platform(path)

    assert (result.platform, result.how, result.folder) == (expected, "folder", expected)


# Every frontend folder name the reviewer probed, and the RomM slug it means.
# The CD Platforms here have no header probe, so the folder is their only signal.
FRONTEND_FOLDERS = [
    ("pcenginecd", "turbografx-cd"), ("tg-cd", "turbografx-cd"),
    ("cdi", "philips-cd-i"), ("cdimono1", "philips-cd-i"),
    ("megadrive", "genesis"), ("genesis-slash-megadrive", "genesis"),
    ("segamd", "genesis"),
    ("mastersystem", "sms"), ("gg", "gamegear"), ("game-gear", "gamegear"),
    ("atarilynx", "lynx"), ("ngp", "neo-geo-pocket"), ("ngpc", "neo-geo-pocket-color"),
    ("wonderswancolor", "wonderswan-color"), ("n3ds", "3ds"),
    ("neogeocd", "neo-geo-cd"), ("pcfx", "pc-fx"), ("mame", "arcade"),
    ("naomi2", "arcade"), ("naomigd", "arcade"),
]


@pytest.mark.parametrize(("folder", "expected"), FRONTEND_FOLDERS)
def test_es_de_and_batocera_folders_name_the_romm_slug(tmp_path, folder, expected):
    path = _blank(tmp_path / folder / "game.bin")

    assert detect_platform(path).platform == expected


def _alias_rows():
    return [
        (alias, slug)
        for slug, aliases in platform_detect._ALIASES.items()
        for alias in (slug, *aliases)
    ]


@pytest.mark.parametrize(("alias", "slug"), _alias_rows())
def test_every_alias_names_its_romm_slug(tmp_path, alias, slug):
    assert platform_detect.canonical_slug(alias) == slug
    path = _blank(tmp_path / alias / "game.bin")
    assert detect_platform(path).platform == slug


# RomM's own slugs for every Platform the tools serve: the values of RomM's
# `UniversalPlatformSlug` enum (rommapp/romm, backend/utils/platform_slugs.py).
# RomM's slug differs from IGDB's in places (`psx`, not `ps`; `genesis`, not
# `genesis-slash-megadrive`), and Detection answers in RomM's.
ROMM_SLUGS = frozenset({
    "3do", "3ds", "arcade", "atari2600", "atari7800", "c64", "dc", "gamegear",
    "gb", "gba", "gbc", "genesis", "laseractive", "lynx", "n64", "nds",
    "neo-geo-cd", "neo-geo-pocket", "neo-geo-pocket-color", "nes",
    "new-nintendo-3ds", "ngc", "pc-fx", "philips-cd-i", "ps2", "ps3", "psp",
    "psx", "saturn", "segacd", "sms", "snes", "switch", "turbografx-cd",
    "virtualboy", "wii", "wiiu", "wonderswan", "wonderswan-color", "xbox",
    "xbox360",
})


@pytest.mark.parametrize("slug", sorted(platform_detect._ALIASES))
def test_every_canonical_key_is_a_romm_slug(slug):
    assert slug in ROMM_SLUGS


def test_aliases_are_unambiguous():
    rows = _alias_rows()
    names = [alias for alias, _slug in rows]
    assert len(names) == len(set(names)), "an alias names two Platforms"


def _declared_slugs():
    from services.tools import registry

    slugs = set()
    for tool in registry.all():
        slugs |= set(tool.platform_slugs)
    for spec in registry.mode_specs():
        slugs |= set(spec.platform_slugs)
    return sorted(slugs)


@pytest.mark.parametrize("declared", _declared_slugs())
def test_every_declared_slug_maps_onto_a_romm_slug_in_the_table(declared):
    # A tool that adds a Platform brings its folder names here too, or this
    # fails: the canonical answer must be a key of the alias table.
    assert platform_detect.canonical_slug(declared) in platform_detect._ALIASES


def test_folder_walk_stops_at_the_volume_root(tmp_path, monkeypatch):
    from config import settings

    # The Volume sits under a folder named like a Platform (`md` is Genesis).
    volume = tmp_path / "mnt" / "md" / "library"
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    headered = _ps2(volume / "pile" / "game.iso")
    blank = _blank(volume / "pile" / "other.iso")

    assert (detect_platform(headered).platform, detect_platform(headered).how) == (
        "ps2", "header",
    )
    assert detect_platform(blank).how == "unsorted"


def test_the_volume_root_itself_can_name_the_platform(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "psx"
    monkeypatch.setattr(settings, "chd_volumes", str(volume))

    assert detect_platform(_blank(volume / "game.iso")).platform == "psx"


def test_a_path_outside_every_volume_uses_only_its_own_folder(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path / "volume"))
    own = _blank(tmp_path / "elsewhere" / "ps2" / "game.iso")
    above = _blank(tmp_path / "elsewhere" / "ps2" / "Game (USA)" / "game.iso")

    assert detect_platform(own).platform == "ps2"
    assert detect_platform(above).how == "unsorted"


def test_nearest_platform_folder_wins(tmp_path):
    path = _write(tmp_path / "ps2" / "psp" / "game.iso", b"\0" * 64)

    assert detect_platform(path).platform == "psp"


def test_nothing_answers_is_unsorted(tmp_path):
    path = _write(tmp_path / "downloads" / "game.iso", b"\0" * 64)

    result = detect_platform(path)

    assert result.platform is None
    assert result.how == "unsorted"
    assert not result.decided


def test_archive_member_uses_the_archive_folder(tmp_path):
    archive = _write(tmp_path / "psx" / "game.zip", b"PK\x05\x06" + b"\0" * 18)

    result = detect_platform(f"{archive}::disc.cue")

    assert (result.platform, result.how) == ("psx", "folder")


def test_missing_file_is_unsorted(tmp_path):
    result = detect_platform(str(tmp_path / "ps2" / "gone.iso"))

    assert result.how == "unsorted"


# --------------------------------------------------------------------------- #
# Precedence and conflicts
# --------------------------------------------------------------------------- #

def _blank(path: Path) -> str:
    return _write(path, b"\0" * 64)


# (folder the Image sits in, header builder, expected platform, expected how)
PRECEDENCE = [
    ("downloads", _ps2, "ps2", "header"),   # header only
    ("ps2", _ps2, "ps2", "header"),         # header and folder agree
    ("ps2", _blank, "ps2", "folder"),       # folder only
    ("gc", _ps2, None, "conflict"),         # a clash names no Platform
    ("ps2", _gamecube, None, "conflict"),
    ("psx", _ps2, None, "conflict"),        # PS1 folder, PS2 disc
    ("downloads", _blank, None, "unsorted"),
]


@pytest.mark.parametrize(("folder", "builder", "platform", "how"), PRECEDENCE)
def test_precedence(tmp_path, folder, builder, platform, how):
    path = builder(tmp_path / folder / "game.iso")

    result = detect_platform(path)

    assert (result.platform, result.how) == (platform, how)


def test_conflict_keeps_both_answers_and_says_why(tmp_path):
    path = _gamecube(tmp_path / "ps2" / "game.iso")

    result = detect_platform(path)

    assert (result.header, result.folder) == ("ngc", "ps2")
    assert "ps2" in result.reason and "ngc" in result.reason


class _Probe:
    """A stub tool whose header probe answers ``slug`` and counts its calls."""

    def __init__(self, tool_id: str, slug: str | None, exts=frozenset({".iso"})):
        self.id = tool_id
        self.slug = slug
        self.input_extensions = exts
        self.platform_slugs = frozenset({slug} if slug else ())
        self.calls = 0

    def header_platform(self, path: str) -> str | None:
        self.calls += 1
        return self.slug


def test_two_headers_that_disagree_are_a_conflict(tmp_path):
    path = _blank(tmp_path / "downloads" / "game.iso")

    result = detect_platform(path, tools=[_Probe("a", "ps2"), _Probe("b", "ngc")])

    assert (result.platform, result.how) == (None, "conflict")


def test_only_tools_taking_the_extension_are_asked(tmp_path):
    path = _blank(tmp_path / "downloads" / "game.iso")
    other = _Probe("cart", "gba", exts=frozenset({".gba"}))

    detect_platform(path, tools=[other])

    assert other.calls == 0


def test_a_failing_probe_is_not_an_answer(tmp_path):
    path = _blank(tmp_path / "ps2" / "game.iso")

    class _Broken(_Probe):
        def header_platform(self, path):
            raise OSError("EIO")

    result = detect_platform(path, tools=[_Broken("x", None)])

    assert (result.platform, result.how) == ("ps2", "folder")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def test_result_is_cached_per_file(tmp_path):
    path = _blank(tmp_path / "downloads" / "game.iso")
    probe = _Probe("a", "ps2")

    detect_platform(path, tools=[probe])
    detect_platform(path, tools=[probe])

    assert probe.calls == 1


def test_cache_is_dropped_when_the_size_changes(tmp_path):
    path = _blank(tmp_path / "downloads" / "game.iso")
    probe = _Probe("a", "ps2")
    detect_platform(path, tools=[probe])

    Path(path).write_bytes(b"\0" * 128)
    detect_platform(path, tools=[probe])

    assert probe.calls == 2


def test_cache_is_dropped_when_the_mtime_changes(tmp_path):
    path = _blank(tmp_path / "downloads" / "game.iso")
    probe = _Probe("a", "ps2")
    detect_platform(path, tools=[probe])

    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    detect_platform(path, tools=[probe])

    assert probe.calls == 2


_SATURN_CUE = b'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n'


def test_a_cue_whose_track_arrives_later_is_detected_once_it_does(tmp_path):
    folder = tmp_path / "downloads"
    cue = _write(folder / "game.cue", _SATURN_CUE)

    assert detect_platform(cue).how == "unsorted"

    _sector0(folder / "game.bin", b"SEGA SEGASATURN ", raw=True)

    assert detect_platform(cue).platform == "saturn"


def test_a_cue_is_re_probed_when_its_track_changes(tmp_path):
    folder = tmp_path / "downloads"
    cue = _write(folder / "game.cue", _SATURN_CUE)
    track = _sector0(folder / "game.bin", b"SEGA SEGASATURN ", raw=True)
    assert detect_platform(cue).platform == "saturn"

    # Same cue sheet, a different disc behind it.
    _sector0(Path(track), b"SEGADISCSYSTEM  ", raw=True)
    st = os.stat(track)
    os.utime(track, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    assert detect_platform(cue).platform == "segacd"


_ACCENTED = "Jeu (\u00c9dition).bin"


@pytest.mark.parametrize(
    "encode",
    [
        lambda text: text.encode("utf-8"),
        lambda text: b"\xef\xbb\xbf" + text.encode("utf-8"),   # UTF-8 with a BOM
        lambda text: text.encode("cp1252"),
    ],
    ids=["utf-8", "utf-8-bom", "cp1252"],
)
def test_a_cue_naming_an_accented_track_is_followed(tmp_path, encode):
    folder = tmp_path / "downloads"
    _sector0(folder / _ACCENTED, b"SEGA SEGASATURN ", raw=True)
    sheet = f'FILE "{_ACCENTED}" BINARY\n  TRACK 01 MODE1/2352\n'
    cue = _write(folder / "game.cue", encode(sheet))

    assert detect_platform(cue).platform == "saturn"


def test_an_unquoted_saturn_cue_is_followed(tmp_path):
    from services.tools import registry

    folder = tmp_path / "downloads"
    track = _sector0(folder / "game.bin", b"SEGA SEGASATURN ", raw=True)
    cue = _write(folder / "game.cue", b"FILE game.bin BINARY\n  TRACK 01 MODE1/2352\n")

    assert detect_platform(cue).platform == "saturn"
    assert registry.get("chdman").header_reads(cue) == [track]


def test_an_unquoted_name_with_spaces_is_read_as_chdman_reads_it(tmp_path):
    # chdman's tokenizer splits on the space: it opens "Game", not the whole
    # name, so the probe follows the same (missing) file and says nothing.
    from services.tools import registry

    folder = tmp_path / "downloads"
    _sector0(folder / "Game (Track 1).bin", b"SEGA SEGASATURN ", raw=True)
    cue = _write(
        folder / "game.cue", b"FILE Game (Track 1).bin BINARY\n  TRACK 01 MODE1/2352\n",
    )

    assert registry.get("chdman").header_reads(cue) == [str(folder / "Game")]
    assert detect_platform(cue).platform is None


def test_a_cp1252_cue_lists_its_accented_track_and_is_cached(tmp_path):
    from services.tools import registry

    folder = tmp_path / "downloads"
    track = _sector0(folder / _ACCENTED, b"SEGA SEGASATURN ", raw=True)
    sheet = f'FILE "{_ACCENTED}" BINARY\n  TRACK 01 MODE1/2352\n'
    cue = _write(folder / "game.cue", sheet.encode("cp1252"))

    # The cache's dependency list reads the sheet the way the probe does.
    assert registry.get("chdman").header_reads(cue) == [track]
    assert detect_platform(cue).platform == "saturn"
    assert cue in platform_detect._cache


@pytest.mark.parametrize(
    ("track_name", "listed"),
    [("Game (Track 1).bin", '"Game (Track 1).bin"'), ("track01.bin", "track01.bin")],
    ids=["quoted-with-spaces", "unquoted"],
)
def test_a_gdi_track_name_is_read_whole(tmp_path, track_name, listed):
    from services.tools import registry

    folder = tmp_path / "stuff"
    raw = bytearray(2352)
    raw[16:16 + 0x200] = _make_ipbin()
    track = _write(folder / track_name, bytes(raw))
    gdi = _write(folder / "game.gdi", f"1\n1 0 4 2352 {listed} 0\n".encode())

    assert detect_platform(gdi).platform == "dc"
    assert registry.get("chdman").header_reads(gdi) == [track]


def test_a_gdi_lists_its_tracks_as_header_reads(tmp_path):
    from services.tools import registry

    folder = tmp_path / "dc"
    gdi = _write(folder / "game.gdi", b"2\n1 0 4 2352 track01.bin 0\n2 600 0 2352 track02.raw 0\n")

    reads = registry.get("chdman").header_reads(gdi)

    assert reads == [str(folder / "track01.bin"), str(folder / "track02.raw")]
