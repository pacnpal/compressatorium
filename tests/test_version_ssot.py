"""SSOT for the application version (#178).

``AGENTS.md`` names ``package.json`` the single source of truth for the
version. These tests pin ``get_version()`` (and the ``/health`` + ``/api/version``
endpoints that surface it) to that file when ``APP_VERSION`` is unset/empty,
while keeping ``APP_VERSION`` as the authoritative container override.
"""

import asyncio
import json
from pathlib import Path

import pytest

import main
from main import get_version, health_check
from routes.info import get_app_version

# Resolve package.json independently of main's own path constant so the test
# genuinely asserts the served version is coupled to the file on disk.
_PACKAGE_JSON = Path(__file__).resolve().parent.parent / "package.json"


def _package_json_version() -> str:
    return json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["version"]


def test_get_version_falls_back_to_package_json(monkeypatch):
    """APP_VERSION unset -> served version is package.json's version, not 'dev'."""
    monkeypatch.delenv("APP_VERSION", raising=False)

    version = get_version()

    assert version == _package_json_version()
    assert version != "dev"  # the regression this closes: never the old default


def test_empty_app_version_is_treated_as_unset(monkeypatch):
    """An empty APP_VERSION ('') falls back to package.json, not served verbatim."""
    monkeypatch.setenv("APP_VERSION", "")

    assert get_version() == _package_json_version()


def test_app_version_env_overrides_package_json(monkeypatch):
    """A non-empty APP_VERSION still wins so container builds stay authoritative."""
    monkeypatch.setenv("APP_VERSION", "9.9.9-from-build-arg")

    assert get_version() == "9.9.9-from-build-arg"


def test_health_endpoint_reports_real_version(monkeypatch):
    """/health surfaces the package.json version in a plain pytest/local run."""
    monkeypatch.delenv("APP_VERSION", raising=False)

    payload = asyncio.run(health_check())

    assert payload["status"] == "healthy"
    assert payload["version"] == _package_json_version()


def test_api_version_endpoint_reports_real_version(monkeypatch):
    """/api/version surfaces the package.json version in a plain pytest/local run."""
    monkeypatch.delenv("APP_VERSION", raising=False)

    payload = asyncio.run(get_app_version())

    assert payload["version"] == _package_json_version()


@pytest.mark.parametrize(
    "bad_content",
    [
        "[]",                  # top-level array -> .get() would AttributeError
        "null",                # top-level null
        "42",                  # top-level number
        '"4.2.0"',             # top-level string
        "{ not valid json",    # unparseable
        '{"version": 42}',     # non-string version
        '{"version": ""}',     # empty version
        "{}",                  # no version key
    ],
)
def test_malformed_or_wrong_shape_package_json_degrades_to_dev(
    monkeypatch, tmp_path, bad_content
):
    """A missing/malformed/wrong-shaped package.json degrades to 'dev', never raises.

    get_version() runs while the FastAPI app is constructed, so a valid-but-
    wrong-shaped file (e.g. a top-level array/null/number) must not raise at
    startup -- it must fall through to the "dev" last resort.
    """
    monkeypatch.delenv("APP_VERSION", raising=False)
    bad = tmp_path / "package.json"
    bad.write_text(bad_content, encoding="utf-8")
    monkeypatch.setattr(main, "_PACKAGE_JSON", bad)

    assert get_version() == "dev"


def test_missing_package_json_degrades_to_dev(monkeypatch, tmp_path):
    """An absent package.json (e.g. a slim container image) yields 'dev', not a crash."""
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(main, "_PACKAGE_JSON", tmp_path / "nope.json")

    assert get_version() == "dev"
