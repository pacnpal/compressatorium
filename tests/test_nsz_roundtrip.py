"""Real nsz round-trip integration test.

Opt-in: it runs the actual ``nsz`` binary through the app's own service code on
a real dump with real keys, doing compress -> verify -> decompress -> compare.
It SKIPS (never fails) unless you supply both, so CI and normal `pytest` runs
stay green.

Supply inputs by either:
  - dropping your `prod.keys` in `testdata/switch/keys/` and a real `.nsp`/`.xci`
    in `testdata/switch/dumps/` (see that folder's README), or
  - setting env vars `SWITCH_KEYS` (keys dir) and `NSZ_ROUNDTRIP_DUMP` (file).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

from app.services import nsz as nsz_module

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTDATA = REPO_ROOT / "testdata" / "switch"
DEFAULT_KEYS_DIR = TESTDATA / "keys"
DEFAULT_DUMPS_DIR = TESTDATA / "dumps"
OUT_DIR = TESTDATA / "out"


def _keys_dir() -> Path | None:
    env = os.environ.get("SWITCH_KEYS")
    return Path(env) if env else (DEFAULT_KEYS_DIR if DEFAULT_KEYS_DIR.is_dir() else None)


def _keys_are_real(keys_dir: Path) -> bool:
    keyfile = keys_dir / "prod.keys"
    if not keyfile.is_file():
        keyfile = keys_dir / "keys.txt"
    if not keyfile.is_file():
        return False
    # The shipped placeholder is tagged; treat it as "no real keys".
    return "REPLACE-ME" not in keyfile.read_text(errors="ignore")


def _find_dump() -> Path | None:
    env = os.environ.get("NSZ_ROUNDTRIP_DUMP")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if not DEFAULT_DUMPS_DIR.is_dir():
        return None
    for child in sorted(DEFAULT_DUMPS_DIR.iterdir()):
        if child.suffix.lower() in (".nsp", ".xci"):
            return child
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _nsz_binary() -> str | None:
    """Resolve the nsz binary: explicit setting, PATH, or the active venv's bin."""
    configured = nsz_module.settings.nsz_path
    if configured != "nsz" and os.path.exists(configured):
        return configured
    found = shutil.which(configured)
    if found:
        return found
    venv_bin = Path(sys.executable).parent / "nsz"
    return str(venv_bin) if venv_bin.exists() else None


async def _run(agen) -> list[dict]:
    return [u async for u in agen]


@pytest.mark.asyncio
async def test_nsz_real_round_trip(monkeypatch):
    nsz_bin = _nsz_binary()
    if nsz_bin is None:
        pytest.skip("nsz binary not found (pip install nsz)")
    # The singleton cached settings.nsz_path ("nsz") at import; set the instance
    # attr so the subprocess uses an absolute path even when the venv bin isn't
    # on PATH for this invocation (in Docker /opt/venv/bin is on PATH).
    monkeypatch.setattr(nsz_module.settings, "nsz_path", nsz_bin)
    monkeypatch.setattr(nsz_module.nsz_service, "nsz_path", nsz_bin)

    keys_dir = _keys_dir()
    if keys_dir is None or not _keys_are_real(keys_dir):
        pytest.skip("no real prod.keys (set SWITCH_KEYS or fill testdata/switch/keys)")

    dump = _find_dump()
    if dump is None:
        pytest.skip("no .nsp/.xci dump (set NSZ_ROUNDTRIP_DUMP or fill testdata/switch/dumps)")

    monkeypatch.setattr(nsz_module.settings, "switch_keys_dir", str(keys_dir))
    assert nsz_module.nsz_service.keys_available()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svc = nsz_module.nsz_service

    # 1. Compress
    compressed = OUT_DIR / Path(svc.get_output_path(str(dump), str(OUT_DIR))).name
    if compressed.exists():
        compressed.unlink()
    updates = await _run(svc.convert(str(dump), str(compressed), "nsz_compress"))
    assert compressed.is_file(), "compression produced no output"
    assert updates[-1]["progress"] == 100
    assert compressed.stat().st_size > 0
    print(f"\ncompressed {dump.name}: "
          f"{dump.stat().st_size} -> {compressed.stat().st_size} bytes")

    # 2. Verify the compressed output with nsz's own check
    verdict = await svc.verify(str(compressed))
    assert verdict["valid"] is True, f"verify failed: {verdict['message']}"

    # 3. Decompress back and 4. compare byte-for-byte
    restored = OUT_DIR / Path(svc.get_output_path(str(compressed), str(OUT_DIR))).name
    if restored.exists():
        restored.unlink()
    await _run(svc.convert(str(compressed), str(restored), "nsz_decompress"))
    assert restored.is_file(), "decompression produced no output"
    assert _sha256(dump) == _sha256(restored), "round trip is not byte-identical"
    print(f"round trip OK: {restored.name} matches original SHA-256")
