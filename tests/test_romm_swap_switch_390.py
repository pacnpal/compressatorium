"""The "keep the ROM id when converting" switch and its token check (#390).

RomM has no endpoint that lists a token's scopes, so turning the switch on asks
three harmless questions (spike #382, RomM 5.4.1) and refuses, naming each
missing permission, when the token fails one. These drive the RomM routes
against :class:`tests.fake_romm.FakeRomm`, which answers the probes from a
configurable set of scopes. The swap itself is #391; this only stores the switch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from routes import romm as romm_routes
from services import db as _db
from services.romm import RommClient
from services.romm import repin as romm_repin
from services.romm import settings as romm_settings

from config import settings as app_settings

from .fake_romm import FakeRomm

WRITE_PROBE = ("PUT", "/api/roms/2147483647")
TASKS_PROBE = ("GET", "/api/tasks/status")


@pytest.fixture(name="romm_db")
def _romm_db(tmp_path: Path, monkeypatch):
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    (tmp_path / "library").mkdir()
    yield tmp_path / "library"
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


async def _connect(fake: FakeRomm, library: Path, *, token: str | None = "rmm_x") -> None:
    patch_ = {"url": fake.url, "library_root": str(library)}
    if token:
        patch_["token"] = token
    await romm_settings.save(patch_)


async def _switch(value: bool):
    return await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(swap_keep_rom_id=value),
    )


async def _status() -> dict:
    with patch.object(romm_repin, "count_pending", return_value=0):
        return await romm_routes.romm_status()


@pytest_asyncio.fixture(name="fake")
async def _fake(romm_db):
    fake = FakeRomm().start()
    await _connect(fake, romm_db)
    yield fake
    fake.stop()


async def _refused() -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _switch(True)
    return caught.value


@pytest.mark.asyncio
async def test_the_switch_is_off_by_default_and_costs_no_probe(fake) -> None:
    assert (await romm_routes.get_romm_settings())["swap_keep_rom_id"] is False
    swap = (await _status())["swap"]
    assert swap == {"keep_rom_id": False, "missing": [], "error": None}
    assert WRITE_PROBE not in fake.requests
    assert TASKS_PROBE not in fake.requests


@pytest.mark.asyncio
async def test_a_token_with_every_scope_turns_it_on_and_it_persists(fake) -> None:
    saved = await _switch(True)

    assert saved["swap_keep_rom_id"] is True
    # The probes the spike found, and no write other than the one probe.
    assert WRITE_PROBE in fake.requests
    assert TASKS_PROBE in fake.requests
    assert ("GET", "/api/roms") in {(m, p.split("?")[0]) for m, p in fake.requests}
    assert [m for m, _ in fake.requests if m == "PUT"] == ["PUT"]
    # Stored with the RomM settings: a reload from the store still has it.
    await romm_settings.load(force=True)
    assert romm_settings.effective()["swap_keep_rom_id"] is True
    assert (await _status())["swap"] == {
        "keep_rom_id": True, "missing": [], "error": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scopes", "named"),
    [
        ({"roms.read", "tasks.run"}, ["roms.write"]),
        ({"roms.read", "roms.write"}, ["tasks.run"]),
        ({"roms.write", "tasks.run"}, ["roms.read"]),
        ({"roms.read"}, ["roms.write", "tasks.run"]),
        (set(), ["roms.read", "roms.write", "tasks.run"]),
    ],
)
async def test_a_missing_scope_refuses_and_names_each_one(fake, scopes, named) -> None:
    fake.scopes = set(scopes)

    err = await _refused()

    assert err.status_code == 422
    for scope in named:
        assert scope in err.detail
    for scope in {"roms.read", "roms.write", "tasks.run"} - set(named):
        assert scope not in err.detail
    assert "admin" in err.detail
    # Refused means not stored.
    assert romm_settings.effective()["swap_keep_rom_id"] is False


@pytest.mark.asyncio
async def test_status_shows_a_permission_problem_once_the_switch_is_on(fake) -> None:
    await _switch(True)
    fake.scopes.discard("tasks.run")

    swap = (await _status())["swap"]

    assert swap["keep_rom_id"] is True
    assert swap["missing"] == ["tasks.run"]
    assert "admin" in swap["error"]


@pytest.mark.asyncio
async def test_a_romm_that_cannot_be_asked_refuses_rather_than_guesses(fake) -> None:
    fake.probe_status = 500

    err = await _refused()

    assert err.status_code == 502
    assert romm_settings.effective()["swap_keep_rom_id"] is False


@pytest.mark.asyncio
async def test_a_rejected_token_is_not_read_as_missing_scopes(fake) -> None:
    fake.probe_status = 401

    err = await _refused()

    assert err.status_code == 502
    assert "roms.write" not in err.detail


@pytest.mark.asyncio
async def test_no_token_refuses_without_asking_romm(romm_db) -> None:
    fake = FakeRomm().start()
    try:
        await _connect(fake, romm_db, token=None)
        err = await _refused()
        assert err.status_code == 422
        assert "token" in err.detail.lower()
        assert WRITE_PROBE not in fake.requests
    finally:
        fake.stop()


@pytest.mark.asyncio
async def test_the_check_uses_the_token_in_the_same_request(fake) -> None:
    """A token typed beside the switch is the one that is checked."""
    seen: list[str | None] = []
    real = RommClient.token_permissions

    def spy(self):
        seen.append(self._explicit_token)
        return real(self)

    with patch.object(RommClient, "token_permissions", spy):
        await romm_routes.put_romm_settings(
            romm_routes.RommSettingsPatch(swap_keep_rom_id=True, token="rmm_new"),
        )

    assert seen == ["rmm_new"]


@pytest.mark.asyncio
async def test_turning_it_off_asks_romm_nothing(fake) -> None:
    await _switch(True)
    fake.requests.clear()
    fake.scopes.clear()

    saved = await _switch(False)

    assert saved["swap_keep_rom_id"] is False
    assert fake.requests == []


def test_the_client_probes_read_the_spike_answers() -> None:
    fake = FakeRomm(scopes={"roms.read", "tasks.run"}).start()
    try:
        client = RommClient(base_url=fake.url, token="rmm_x")
        assert client.token_permissions() == {
            "roms.read": True, "roms.write": False, "tasks.run": True,
        }
    finally:
        fake.stop()


def test_each_probe_carries_its_own_short_timeout() -> None:
    timeouts: list[float] = []

    def opener(_req, _origin, timeout=30):
        timeouts.append(timeout)
        raise AssertionError("stop here")

    with patch("services.romm.client._urlopen", opener), pytest.raises(AssertionError):
        RommClient(base_url="http://romm:8080", token="t").token_permissions()
    assert timeouts
    assert all(0 < t <= 15 for t in timeouts)
