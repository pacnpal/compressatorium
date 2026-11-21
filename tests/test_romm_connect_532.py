"""RomM connects itself (#532): discovery, pairing code, library path, RomM's
own structure and exclusions, and a connection test that names the field.

Every RomM here is :class:`FakeRomm`, answering in RomM's own shapes (see its
docstring for the sources).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from routes import romm as romm_routes
from services import db as _db
from services.romm import RommClient, RommError
from services.romm import connect as romm_connect
from services.rules.library import PlacementUnknown

from config import settings as app_settings

from .fake_romm import FakeRomm

# The sweeps here read RomM's layout from the fake (see conftest).
READS_ROMM_LAYOUT = True

TOKEN = {
    "id": 3, "name": "compressatorium", "scopes": ["platforms.read", "roms.read"],
    "expires_at": None, "last_used_at": None, "created_at": "2026-11-20T00:00:00Z",
    "user_id": 1, "raw_token": "rmm_" + "a" * 64,
}


@pytest.fixture(name="romm_db")
def _romm_db(tmp_path: Path, monkeypatch):
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    romm_connect.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    yield tmp_path
    romm_settings.reset_for_tests()
    romm_connect.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture(name="fake")
def _fake():
    fake = FakeRomm(
        version="5.4.1", fs_platforms=["snes", "gba"],
        structure="roms/{platform}/{game}", pair_codes={"ABCD2345": TOKEN},
        excluded={"EXCLUDED_SINGLE_EXT": ["bak"], "EXCLUDED_SINGLE_FILES": ["*.nfo"]},
    ).start()
    yield fake
    fake.stop()


# ---- pairing ----------------------------------------------------------------


def test_the_client_trades_a_pairing_code_for_its_token(fake):
    client = RommClient(base_url=fake.url, token="")
    # Dashes and case don't matter, as in RomM.
    assert client.exchange_pair_code("abcd-2345")["raw_token"] == TOKEN["raw_token"]
    # Single use.
    with pytest.raises(RommError) as caught:
        client.exchange_pair_code("ABCD2345")
    assert caught.value.status == 404


@pytest.mark.asyncio
async def test_pairing_holds_the_token_until_the_save_and_never_returns_it(romm_db, fake):
    """A pairing changes nothing on its own (Codex PRRT_kwDOQ9nULM66RDFs): the
    token waits for the Save that commits it with the URL and library path, so
    a library path that then fails its test leaves the old connection whole."""
    from fastapi import HTTPException
    from services.romm import settings as romm_settings

    await romm_settings.save({"url": "http://old-romm:8080", "token": "rmm_old"})
    answer = await romm_routes.pair_romm(
        romm_routes.PairRequest(url=fake.url, code="ABCD-2345"),
    )

    assert TOKEN["raw_token"] not in repr(answer)
    # What the token can't do yet, so the user knows before a feature fails.
    assert answer["missing_scopes"] == ["roms.write", "tasks.run"]
    assert romm_settings.effective()["url"] == "http://old-romm:8080"
    assert romm_settings.token() == "rmm_old"

    # The test and the Save use the held token, for that URL only.
    tested = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=fake.url, paired=True),
    )
    assert tested["authorized"] is True, tested
    with pytest.raises(HTTPException) as caught:
        await romm_routes.put_romm_settings(
            romm_routes.RommSettingsPatch(url="http://elsewhere:8080", paired=True),
        )
    assert caught.value.status_code == 400
    assert romm_settings.effective()["url"] == "http://old-romm:8080"

    saved = await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(url=fake.url, paired=True),
    )
    assert saved["token_set"] is True
    assert romm_settings.effective()["url"] == fake.url
    assert romm_settings.token() == TOKEN["raw_token"]
    # Spent by the Save: a second one has nothing to use.
    with pytest.raises(HTTPException):
        await romm_routes.put_romm_settings(
            romm_routes.RommSettingsPatch(url=fake.url, paired=True),
        )


@pytest.mark.asyncio
async def test_a_wrong_pairing_code_says_to_get_a_new_one(romm_db, fake):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await romm_routes.pair_romm(romm_routes.PairRequest(url=fake.url, code="ZZZZ9999"))
    assert caught.value.status_code == 400
    assert "Pair" in caught.value.detail


# ---- discovery --------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_finds_romm_and_its_library(romm_db, fake):
    library = romm_db / "romm" / "library"
    (library / "roms" / "snes").mkdir(parents=True)
    (library / "roms" / "gba").mkdir()

    found = await romm_connect.discover(("http://127.0.0.1:1", fake.url), timeout=2)

    assert found == {"url": fake.url, "version": "5.4.1", "library_root": str(library)}
    # Only the public heartbeat was asked: nothing about the owner is sent.
    assert fake.paths() == ["/api/heartbeat"]


@pytest.mark.asyncio
async def test_discovery_with_nothing_answering_finds_nothing(romm_db):
    assert await romm_connect.discover(("http://127.0.0.1:1",), timeout=2) is None


def test_the_library_path_follows_romms_platform_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    # RomM's library, and a decoy holding one of the names.
    (tmp_path / "media" / "romm" / "roms" / "snes").mkdir(parents=True)
    (tmp_path / "media" / "romm" / "roms" / "gba").mkdir()
    (tmp_path / "other" / "snes").mkdir(parents=True)

    found = romm_connect.find_library_root(["snes", "gba"])

    assert found == str(tmp_path / "media" / "romm")
    assert romm_connect.find_library_root(["n64"]) is None


# ---- the connection test ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_romm_fails_only_the_check_it_ran(romm_db):
    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url="http://127.0.0.1:1", token="rmm_x"),
    )
    assert result["reachable"] is False
    # Not tried, so not failed.
    assert result["authorized"] is None
    assert result["library_root_mounted"] is None
    assert result["field"] == "url"


@pytest.mark.asyncio
async def test_no_token_names_the_token_field(romm_db, fake):
    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=fake.url),
    )
    assert result["reachable"] is True
    assert result["authorized"] is False
    assert result["field"] == "token"
    assert "ROMM_" not in result["error"]


@pytest.mark.asyncio
async def test_the_connection_test_suggests_the_library_path(romm_db, fake):
    library = romm_db / "lib"
    (library / "roms" / "snes").mkdir(parents=True)

    result = await romm_routes.test_romm_connection(
        romm_routes.RommSettingsPatch(url=fake.url, token="rmm_x"),
    )

    assert result["authorized"] is True
    assert result["library_root_mounted"] is None
    assert result["library_root_suggestion"] == str(library)
    assert result["field"] == "library_root"


# ---- RomM's structure and exclusions ------------------------------------------


@pytest.mark.parametrize(("template", "parsed"), [
    ("roms/{platform}/{game}", (("roms",), ())),
    ("{platform}/roms/{game}", ((), ("roms",))),
    ("roms/{platform}/{category}/{game}", (("roms",), (None,))),
    ("{game}", None),
    ("{x}/{platform}/{game}", None),
    ("roms/../{platform}/{game}", None),
])
def test_romms_structure_templates_parse(template, parsed):
    assert romm_connect.parse_structure(template) == parsed


@pytest.mark.asyncio
async def test_the_layout_is_read_from_romm(romm_db, fake):
    from services.romm import settings as romm_settings

    await romm_settings.save({"url": fake.url, "token": "rmm_x"})
    await romm_connect.refresh_layout()

    assert romm_connect.structure() == (("roms",), ())
    assert romm_connect.excluded("Game.BAK")
    assert romm_connect.excluded("Game.nfo")
    assert not romm_connect.excluded("Game.iso")


@pytest.fixture(name="romm_root")
def _romm_root(tmp_path, monkeypatch):
    from services.romm import client as romm_client_module

    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str(root)),
    )
    monkeypatch.setattr(romm_client_module.romm_client, "platforms", lambda: [
        {"id": 3, "slug": "ps2", "fs_slug": "ps2"},
    ])
    return root


def test_placement_follows_romms_declared_structure(romm_root):
    from services.romm.library import romm_library

    # Nothing on disk to tell by: RomM says where.
    with patch.object(romm_connect, "structure", return_value=((), ("roms",))):
        placed = romm_library.placement("ps2")
    assert placed.folder == str(romm_root / "ps2" / "roms")
    assert placed.guessed is False


def test_placement_refuses_a_structure_with_a_folder_per_game(romm_root):
    from services.romm.library import romm_library

    with patch.object(romm_connect, "structure", return_value=(("roms",), (None,))), \
            pytest.raises(PlacementUnknown):
        romm_library.placement("ps2")


@pytest.mark.asyncio
async def test_a_rule_skips_files_romm_excludes(romm_db, fake):
    """A file RomM ignores is never converted, and a target format RomM
    ignores isn't written at all (the game would vanish from RomM)."""
    from services.job_manager import job_manager
    from services.romm import auto as romm_auto
    from services.romm import settings as romm_settings
    from services.rules import sweep as rules_sweep

    lib = romm_db / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Skip.iso").write_bytes(b"\0" * 32)
    await romm_settings.save({"url": fake.url, "library_root": str(lib.parents[1])})
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})
    roms = [
        {"id": n, "name": name, "full_path": f"roms/gc/{name}.iso", "fs_name": f"{name}.iso",
         "fs_size_bytes": 32, "platform_slug": "ngc"}
        for n, name in ((1, "Game"), (2, "Skip"))
    ]

    def ignores(name: str) -> bool:
        return name == "Skip.iso"

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True), \
            patch.object(romm_connect, "excluded", side_effect=ignores):
        preview = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
    assert preview["queued"] == 1, preview
    assert preview["skipped_filtered"] == 1, preview

    with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
            patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
            patch.object(rules_sweep, "is_within_configured_volumes", return_value=True), \
            patch.object(romm_connect, "excluded_extension", side_effect=lambda e: e == "rvz"):
        refused = await romm_auto.sweep(ignore_schedule=True, dry_run=True)
    assert refused["queued"] == 0, refused
    assert {"platform_id": 7, "error": "output_excluded"} in refused["errors"], refused


@pytest.mark.asyncio
async def test_a_filename_exclusion_skips_that_output_not_the_rule(romm_db, fake):
    """RomM excluding one file name skips the game whose output has that name,
    not the whole Rule (Codex PRRT_kwDOQ9nULM66RDFy); excluding the extension
    still refuses the Rule."""
    from services.job_manager import job_manager
    from services.romm import auto as romm_auto
    from services.romm import settings as romm_settings
    from services.rules import sweep as rules_sweep

    lib = romm_db / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    roms = []
    for n, name in ((1, "Game"), (2, "output"), (3, "Other")):
        (lib / f"{name}.iso").write_bytes(b"\0" * 32)
        roms.append({
            "id": n, "name": name, "full_path": f"roms/gc/{name}.iso",
            "fs_name": f"{name}.iso", "fs_size_bytes": 32, "platform_slug": "ngc",
        })
    await romm_settings.save(
        {"url": fake.url, "token": "rmm_x", "library_root": str(lib.parents[1])},
    )
    await romm_auto.set_rules({"7": {"mode": "dolphin_rvz", "enabled": True}})

    async def preview(excluded: dict) -> dict:
        fake.excluded = excluded
        await romm_connect.refresh_layout(force=True)
        with patch.object(romm_routes.romm_client, "roms", return_value=roms), \
                patch.object(job_manager, "get_active_job_candidates", return_value=[]), \
                patch.object(rules_sweep, "is_within_configured_volumes", return_value=True):
            return await romm_auto.sweep(ignore_schedule=True, dry_run=True)

    named = await preview({"EXCLUDED_SINGLE_FILES": ["output.rvz", "Other.rvz"]})
    assert named["queued"] == 1, named
    assert named["skipped_filtered"] == 2, named
    assert not any(e["error"] == "output_excluded" for e in named["errors"]), named

    by_ext = await preview({"EXCLUDED_SINGLE_EXT": ["rvz"]})
    assert by_ext["queued"] == 0, by_ext
    assert {"platform_id": 7, "error": "output_excluded"} in by_ext["errors"], by_ext


@pytest.mark.asyncio
async def test_an_unattended_sweep_reads_romms_layout(romm_db, fake):
    """The scheduler's and run-rules' sweeps read RomM's layout themselves
    (Codex PRRT_kwDOQ9nULM66RDFw): nobody has to open the RomM page first."""
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library
    from services.rules import sweep as rules_sweep

    await romm_settings.save({"url": fake.url, "token": "rmm_x"})
    assert romm_connect.layout() == {}

    with patch.object(romm_routes.romm_client, "roms", return_value=[]):
        await rules_sweep.sweep(romm_library, dry_run=True)

    assert romm_connect.structure() == (("roms",), ())
    assert romm_connect.excluded("Game.BAK")


@pytest.mark.asyncio
async def test_an_inbox_poll_reads_the_librarys_layout_before_placing():
    """Placement from an Inbox reads the Library's layout first (Codex
    PRRT_kwDOQ9nULM66RDFw), as the sweep does."""
    from unittest.mock import AsyncMock, MagicMock

    from services import libraries
    from services.inbox import intake

    library = MagicMock()
    library.refresh = AsyncMock()
    inbox = MagicMock(id="inbox-refresh", library_id="lib", folder="/nowhere")
    with patch.object(libraries, "get", return_value=library), \
            patch.object(intake, "_settle_flights", AsyncMock()), \
            patch.object(intake, "run_detached_within_stall_bound",
                         AsyncMock(side_effect=intake.InboxUnavailable("gone"))):
        await intake.poll(inbox)
    library.refresh.assert_awaited_once()


# ---- a layout read that got nothing ---------------------------------------------


def _layout_client(*, answers: bool):
    """A RommClient stand-in whose layout calls fail, or answer, counted."""
    from unittest.mock import MagicMock

    client = MagicMock(base_url="http://romm.test")
    if answers:
        client.config.return_value = {}
        client.library_structure.return_value = "roms/{platform}/{game}"
    else:
        client.config.side_effect = RommError("timed out")
        client.library_structure.side_effect = RommError("timed out")
    return client


@pytest.mark.parametrize(("answers", "reads"), [(False, 2), (True, 1)])
def test_a_layout_read_that_got_nothing_is_retried_soon(monkeypatch, answers, reads):
    """A read where every call failed is retried after a short wait, not the
    full TTL; one that got an answer keeps the TTL (CodeRabbit
    PRRT_kwDOQ9nULM66RLv7)."""
    romm_connect.reset_for_tests()
    monkeypatch.setattr(romm_connect, "_LAYOUT_RETRY", 0.0)
    client = _layout_client(answers=answers)

    romm_connect.refresh_layout_now(client)
    romm_connect.refresh_layout_now(client)

    assert client.config.call_count == reads
    romm_connect.reset_for_tests()


@pytest.mark.asyncio
async def test_a_layout_never_read_defers_the_sweep_and_placement(romm_db, romm_root):
    """RomM's layout was never read (RomM unreachable since the start): the
    sweep is deferred and placement refused, never run as if RomM excluded
    nothing and declared no structure (CodeRabbit PRRT_kwDOQ9nULM66RLv9)."""
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library
    from services.rules import sweep as rules_sweep

    await romm_settings.save({"url": "http://127.0.0.1:1", "token": "rmm_x"})

    result = await rules_sweep.sweep(romm_library, dry_run=True)

    assert result["queued"] == 0
    assert {"platform_id": None, "error": "romm_layout_unread"} in result["errors"], result
    with pytest.raises(PlacementUnknown):
        romm_library.placement("ps2")


def test_the_platform_folder_follows_the_declared_prefix(romm_root):
    """``games/{platform}/{game}``: the Platform folder is ``games/gc``, not
    ``games`` (CodeRabbit PRRT_kwDOQ9nULM66RLwA); RomM's standard structure
    keeps its answer."""
    from services.romm.library import romm_library

    (romm_root / "games" / "gc").mkdir(parents=True)
    game = romm_root / "games" / "gc" / "Game.rvz"
    game.write_bytes(b"x")
    with patch.object(romm_connect, "structure", return_value=(("games",), ())):
        assert romm_library.platform_folder(str(game)) == str(romm_root / "games" / "gc")

    (romm_root / "roms" / "gc").mkdir(parents=True)
    other = romm_root / "roms" / "gc" / "Other.rvz"
    other.write_bytes(b"x")
    with patch.object(romm_connect, "structure", return_value=(("roms",), ())):
        assert romm_library.platform_folder(str(other)) == str(romm_root / "roms" / "gc")
