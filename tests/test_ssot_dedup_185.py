"""Tests for the #185 SSOT de-duplication seams.

Each backend fact that was defined in 2-5 places now has one definition; these
lock that in. The broad behavior is covered by the existing route/queue suites
(which stay green because the refactor is behavior-preserving).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.models import (
    BasicFileInfo,
    ConversionJob,
    ConversionMode,
    CsoInfo,
    JobStatus,
    NszInfo,
    Ps3IsoInfo,
    RomzInfo,
    Z3DSInfo,
)
from app.routes import convert as convert_routes
from app.services.archive import ARCHIVE_EXTENSIONS, ArchiveService
from app.services.job_manager import _is_active_conversion, _is_conversion_job
from app.services.tools import registry
from app.services.tools.base import BaseTool


# --- Item 1: shared request validation (single + batch use one path) ----------

def test_validate_request_compression_rejects_unsupported():
    spec = registry.spec("dolphin_iso")
    with pytest.raises(HTTPException) as ei:
        convert_routes._validate_request_compression(spec, "dolphin_iso", "zstd")
    assert ei.value.status_code == 400
    # no compression requested -> no-op
    convert_routes._validate_request_compression(spec, "dolphin_iso", None)


def test_validate_request_compression_allows_supported():
    # chdman create accepts a codec -> no raise.
    convert_routes._validate_request_compression(registry.spec("createcd"), "createcd", "zlib")


def test_validate_delete_on_verify():
    unsupported = registry.spec("romz_extract")  # supports_delete_on_verify=False
    with pytest.raises(HTTPException) as ei:
        convert_routes._validate_delete_on_verify(unsupported, True)
    assert ei.value.status_code == 400
    assert ei.value.detail == convert_routes._DELETE_ON_VERIFY_UNSUPPORTED_DETAIL
    # a supported mode, or delete_on_verify not requested -> no-op
    convert_routes._validate_delete_on_verify(registry.spec("createcd"), True)
    convert_routes._validate_delete_on_verify(unsupported, False)


# --- Items 2 & 3: single-definition strings / tokens --------------------------

def test_delete_on_verify_detail_is_single_constant():
    assert "Delete-on-verify is only supported" in (
        convert_routes._DELETE_ON_VERIFY_UNSUPPORTED_DETAIL
    )


def test_confirmation_tokens_match_frontend():
    # Must equal the values the frontend mirrors in src/lib/api/client.js (CONFIRM).
    assert convert_routes.ACTION_CONFIRM_HEADER == "x-chd-action-confirm"
    assert convert_routes.CONFIRM_CANCEL_ALL_JOBS == "cancel-all-jobs"
    assert convert_routes.CONFIRM_CLEAR_COMPLETED_JOBS == "clear-completed-jobs"


# --- Item 4: BasicFileInfo base + shared raw->model mapping -------------------

@pytest.mark.parametrize("cls", [Z3DSInfo, NszInfo, CsoInfo, RomzInfo, Ps3IsoInfo])
def test_simple_info_models_subclass_basic(cls):
    assert issubclass(cls, BasicFileInfo)
    base_fields = {
        "file", "size", "size_display", "format",
        "extension", "compressed", "compression_type",
    }
    assert base_fields <= set(cls.model_fields)


def test_info_model_extras_preserved():
    assert {"contained_name", "original_size", "ratio"} <= set(RomzInfo.model_fields)
    assert {"title", "title_id"} <= set(Ps3IsoInfo.model_fields)


def test_basic_info_fields_maps_required_and_optional():
    raw = {
        "file": "/g.cso", "size": 5, "size_display": "5 B", "format": "CSO",
        "extension": ".cso", "compressed": True, "compression_type": "cso",
    }
    assert BaseTool._basic_info_fields(raw) == raw
    # Required fields are indexed directly; absent optionals become None.
    out = BaseTool._basic_info_fields(
        {"file": "/g", "size": 1, "size_display": "1 B",
         "extension": ".x", "compressed": False},
    )
    assert out["format"] is None
    assert out["compression_type"] is None


def test_tool_info_model_uses_shared_fields():
    raw = {
        "file": "/g.cso", "size": 5, "size_display": "5 B", "format": "CSO",
        "extension": ".cso", "compressed": True, "compression_type": "cso",
    }
    model = registry.for_mode("cso_compress").info_model(raw, "/g.cso")
    # (class identity is checked structurally — under PYTHONPATH=app the tool
    # builds ``models.CsoInfo`` while the test imports ``app.models.CsoInfo``,
    # two distinct objects, so compare by name + the shared-field values.)
    assert type(model).__name__ == "CsoInfo"
    assert (model.file, model.size, model.compressed) == ("/g.cso", 5, True)
    assert model.format == "CSO"
    assert model.compression_type == "cso"


# --- Item 5: registry-authoritative archive extensions (no static fallback) ---

def test_listable_extensions_from_registry():
    expected = (
        registry.convertible_extensions()
        | registry.archive_input_extensions()
    ) - ARCHIVE_EXTENSIONS
    assert ArchiveService._listable_extensions() == expected
    # Archive containers are never listable members.
    assert not (ArchiveService._listable_extensions() & ARCHIVE_EXTENSIONS)


def test_convert_gate_extensions_from_registry():
    assert ArchiveService._convert_gate_extensions() == registry.archive_input_extensions()


def test_listable_extensions_fails_loudly_on_empty_registry(monkeypatch):
    # A broken/unloaded registry (empty union) must raise, not silently return an
    # empty set that renders as an "empty archive". (archive.py uses the
    # no-app-prefix ``services.tools.registry``, so patch that one.)
    from services.tools import registry as reg

    monkeypatch.setattr(reg, "convertible_extensions", frozenset)
    monkeypatch.setattr(reg, "archive_input_extensions", frozenset)
    with pytest.raises(RuntimeError):
        ArchiveService._listable_extensions()
    with pytest.raises(RuntimeError):
        ArchiveService._convert_gate_extensions()


# --- Item 6: the single live-conversion predicate -----------------------------

def _job(mode, status):
    return ConversionJob(
        id="x", file_path="/f", filename="f", mode=mode, status=status,
        created_at=datetime.now(timezone.utc),
    )


def test_is_conversion_job_excludes_external():
    assert _is_conversion_job(
        _job(ConversionMode.METADATA_SCAN, JobStatus.PROCESSING)
    ) is False
    assert _is_conversion_job(_job(ConversionMode.CREATECD, JobStatus.QUEUED)) is True


def test_is_active_conversion():
    assert _is_active_conversion(_job(ConversionMode.CREATECD, JobStatus.QUEUED)) is True
    assert _is_active_conversion(_job(ConversionMode.CREATECD, JobStatus.PROCESSING)) is True
    # A finished conversion, or any external job, is not an active conversion.
    assert _is_active_conversion(_job(ConversionMode.CREATECD, JobStatus.COMPLETED)) is False
    assert _is_active_conversion(
        _job(ConversionMode.METADATA_SCAN, JobStatus.PROCESSING)
    ) is False
