"""Tests for igir API routes."""
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, Mock, patch

from app.models import DatFileEntry, IgirCommand, IgirJob, IgirJobCreateRequest, IgirValidationResult, JobStatus
from app.routes import igir as igir_routes
from services.job_manager import QueueBackpressureError


@pytest.fixture
def igir_test_env(tmp_path, monkeypatch):
    """Set up test environment for igir routes."""
    roms_dir = tmp_path / "roms"
    roms_dir.mkdir()
    output_dir = tmp_path / "sorted"
    output_dir.mkdir()
    dats_dir = tmp_path / "dats"
    dats_dir.mkdir()

    monkeypatch.setattr(igir_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(igir_routes.settings, "data_mount_root", str(tmp_path))
    monkeypatch.setattr(igir_routes.settings, "igir_dat_path", str(dats_dir))

    return {
        "roms_dir": str(roms_dir),
        "output_dir": str(output_dir),
        "dats_dir": str(dats_dir),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_igir_service(monkeypatch):
    """Mock the igir_service for testing."""
    mock_svc = Mock()
    mock_svc.validate_request = Mock(return_value=IgirValidationResult(
        valid=True,
        errors=[],
        warnings=[],
        command_preview="igir copy --input /data/roms --output /data/out",
    ))
    monkeypatch.setattr(igir_routes, "igir_service", mock_svc)
    return mock_svc


@pytest.fixture
def mock_job_manager(monkeypatch):
    """Mock the job_manager for testing."""
    from datetime import datetime, timezone

    mock_jm = Mock()

    async def fake_create_igir_job(request):
        return IgirJob(
            id="abc12345",
            commands=list(request.commands),
            input_paths=list(request.input_paths),
            output_path=request.output_path,
            status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
        )

    mock_jm.create_igir_job = fake_create_igir_job
    mock_jm.get_all_igir_jobs = Mock(return_value=[])
    mock_jm.get_igir_job = Mock(return_value=None)
    mock_jm.cancel_igir_job = AsyncMock(return_value=True)
    mock_jm.delete_igir_job = AsyncMock(return_value=True)
    mock_jm.cancel_all_igir_jobs = AsyncMock(return_value={"requested": 0, "queued": 0, "processing": 0})
    mock_jm.clear_completed_igir_jobs = AsyncMock(return_value=[])

    monkeypatch.setattr(igir_routes, "job_manager", mock_jm)
    return mock_jm


# ──────────────── Job CRUD ────────────────


@pytest.mark.asyncio
async def test_create_igir_job_valid(igir_test_env, mock_igir_service, mock_job_manager):
    """Create a valid igir job."""
    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
    )
    result = await igir_routes.create_igir_job(request)
    assert result.id == "abc12345"
    assert result.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_create_igir_job_invalid_rejected(igir_test_env, mock_igir_service, mock_job_manager):
    """Reject invalid igir job request."""
    from fastapi import HTTPException

    mock_igir_service.validate_request.return_value = IgirValidationResult(
        valid=False,
        errors=["No commands specified"],
        warnings=[],
        command_preview="",
    )

    request = IgirJobCreateRequest(
        commands=[],
        input_paths=[igir_test_env["roms_dir"]],
    )

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.create_igir_job(request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_igir_job_backpressure_returns_429(
    igir_test_env, mock_igir_service, mock_job_manager
):
    """Queue saturation returns 429 with backpressure details."""
    from fastapi import HTTPException

    async def _raise_backpressure(_request):
        raise QueueBackpressureError(
            current_depth=1,
            max_depth=1,
            additional_jobs=1,
        )

    mock_job_manager.create_igir_job = _raise_backpressure

    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
    )

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.create_igir_job(request)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_list_igir_jobs(igir_test_env, mock_job_manager):
    """List igir jobs returns empty list."""
    result = await igir_routes.list_igir_jobs()
    assert result == []


@pytest.mark.asyncio
async def test_get_igir_job_not_found(igir_test_env, mock_job_manager):
    """Get non-existent igir job returns 404."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.get_igir_job("nonexistent")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_igir_job_not_found(igir_test_env, mock_job_manager):
    """Cancel non-existent igir job returns 404."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.cancel_igir_job("nonexistent")

    assert exc_info.value.status_code == 404


# ──────────────── Validation ────────────────


@pytest.mark.asyncio
async def test_validate_request(igir_test_env, mock_igir_service):
    """Validate igir request returns validation result."""
    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
    )
    result = await igir_routes.validate_igir_request(request)
    assert result.valid is True
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_dry_run_sets_flag(igir_test_env, mock_igir_service):
    """Dry run sets clean_dry_run flag."""
    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
    )
    result = await igir_routes.igir_dry_run(request)
    assert result.valid is True
    assert any("dry-run" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_quick_setup_recommends_matching_dats(igir_test_env, monkeypatch):
    """Quick setup should return practical defaults and DAT matches."""
    platform_dir = igir_test_env["tmp_path"] / "Nintendo Switch"
    platform_dir.mkdir()

    dat_entries = [
        DatFileEntry(
            name="nintendo-switch-no-intro.dat",
            path=f"{igir_test_env['dats_dir']}/nintendo-switch-no-intro.dat",
            size=1024,
            modified="2026-01-01T00:00:00Z",
        ),
        DatFileEntry(
            name="sony-playstation-redump.dat",
            path=f"{igir_test_env['dats_dir']}/sony-playstation-redump.dat",
            size=2048,
            modified="2026-01-01T00:00:00Z",
        ),
    ]
    monkeypatch.setattr(
        igir_routes.igir_service,
        "search_dats",
        AsyncMock(return_value=dat_entries),
    )

    result = await igir_routes.igir_quick_setup(
        igir_routes.IgirQuickSetupRequest(input_paths=[str(platform_dir)]),
    )

    assert result["commands"] == ["copy", "zip", "test"]
    assert result["single"] is False
    assert result["dir_dat_name"] is True
    assert result["filter_preset"] == "retail"
    assert result["workflow_id"] == "first_sort"
    assert isinstance(result.get("workflows"), list)
    assert len(result["workflows"]) > 0
    assert dat_entries[0].path in result["dat_paths"]
    assert result["output_path"].endswith("Nintendo Switch_organized")


@pytest.mark.asyncio
async def test_quick_setup_selects_goal_workflow(igir_test_env, monkeypatch):
    """Quick setup returns selected workflow defaults when goal is provided."""
    platform_dir = igir_test_env["tmp_path"] / "Nintendo Switch"
    platform_dir.mkdir()
    monkeypatch.setattr(
        igir_routes.igir_service,
        "search_dats",
        AsyncMock(return_value=[]),
    )

    result = await igir_routes.igir_quick_setup(
        igir_routes.IgirQuickSetupRequest(
            input_paths=[str(platform_dir)],
            goal="playlist_only",
        ),
    )

    assert result["workflow_id"] == "playlist_only"
    assert result["commands"] == ["playlist"]
    assert result["requires_dats"] is False


@pytest.mark.asyncio
async def test_quick_setup_accepts_file_inputs(igir_test_env, monkeypatch):
    """Quick setup should accept file paths in addition to directories."""
    rom_file = igir_test_env["tmp_path"] / "roms" / "Pokemon - Blue.gb"
    rom_file.write_text("rom-bytes")
    monkeypatch.setattr(
        igir_routes.igir_service,
        "search_dats",
        AsyncMock(return_value=[]),
    )

    result = await igir_routes.igir_quick_setup(
        igir_routes.IgirQuickSetupRequest(input_paths=[str(rom_file)]),
    )

    assert result["input_count"] == 1
    assert result["workflow_id"] == "first_sort"
    assert result["output_path"].endswith("roms_organized")


@pytest.mark.asyncio
async def test_quick_setup_accepts_glob_inputs(igir_test_env, monkeypatch):
    """Quick setup should accept glob-based inputs."""
    monkeypatch.setattr(
        igir_routes.igir_service,
        "search_dats",
        AsyncMock(return_value=[]),
    )
    glob_input = f"{igir_test_env['roms_dir']}/**/*.zip"

    result = await igir_routes.igir_quick_setup(
        igir_routes.IgirQuickSetupRequest(input_paths=[glob_input]),
    )

    assert result["input_count"] == 1
    assert result["workflow_id"] == "first_sort"
    assert result["output_path"].endswith("roms_organized")


@pytest.mark.asyncio
async def test_quick_setup_output_stays_within_selected_directory(igir_test_env, monkeypatch):
    """Quick setup should recommend an output path inside the selected directory tree."""
    monkeypatch.setattr(
        igir_routes.igir_service,
        "search_dats",
        AsyncMock(return_value=[]),
    )
    input_dir = Path(igir_test_env["roms_dir"])

    result = await igir_routes.igir_quick_setup(
        igir_routes.IgirQuickSetupRequest(input_paths=[str(input_dir)]),
    )

    output_dir = Path(result["output_path"])
    assert output_dir != input_dir
    assert output_dir.is_relative_to(input_dir)


@pytest.mark.asyncio
async def test_quick_setup_rejects_invalid_inputs(igir_test_env):
    """Quick setup should fail when no valid input paths are provided."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.igir_quick_setup(
            igir_routes.IgirQuickSetupRequest(input_paths=["/outside/volumes"]),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_track_feature_event_counts():
    """Feature events should accumulate counts for supported event names."""
    igir_routes._feature_event_counts.clear()

    first = await igir_routes.track_igir_feature_event(
        igir_routes.IgirFeatureEventRequest(event="igir_autoconfig_applied"),
    )
    second = await igir_routes.track_igir_feature_event(
        igir_routes.IgirFeatureEventRequest(event="igir_autoconfig_applied"),
    )
    snapshot = await igir_routes.list_igir_feature_events()

    assert first["total"] == 1
    assert second["total"] == 2
    assert snapshot["events"]["igir_autoconfig_applied"] == 2


@pytest.mark.asyncio
async def test_track_feature_event_rejects_unknown_event():
    """Unknown feature event names should be rejected."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.track_igir_feature_event(
            igir_routes.IgirFeatureEventRequest(event="unknown_event"),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_preflight_detects_destructive_flow(igir_test_env, monkeypatch):
    """Preflight marks move/clean/overwrite flows as requiring confirmation."""
    monkeypatch.setattr(
        "utils.path_utils.is_within_configured_volumes",
        lambda *a, **kw: True,
    )
    request = IgirJobCreateRequest(
        commands=[IgirCommand.MOVE, IgirCommand.CLEAN],
        input_paths=[igir_test_env["roms_dir"], igir_test_env["output_dir"]],
        output_path=igir_test_env["output_dir"],
        dat_paths=[f"{igir_test_env['dats_dir']}/set.dat"],
        overwrite=True,
    )

    result = await igir_routes.igir_preflight(request)
    assert result["requires_confirmation"] is True
    assert result["valid"] is True
    assert isinstance(result.get("risk_factors"), list)


@pytest.mark.asyncio
async def test_preflight_writable_target_uses_existing_parent(igir_test_env, monkeypatch):
    """Preflight should check writability against the nearest existing parent."""
    monkeypatch.setattr(
        "utils.path_utils.is_within_configured_volumes",
        lambda *a, **kw: True,
    )
    nested_output = igir_test_env["tmp_path"] / "new" / "nested" / "sorted"
    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=str(nested_output),
    )

    result = await igir_routes.igir_preflight(request)

    assert result["valid"] is True
    assert result["path_checks"]["output"]["exists"] is False
    assert result["path_checks"]["output"]["writable"] is True
    assert result["path_checks"]["output"]["writable_target"] == str(igir_test_env["tmp_path"])


@pytest.mark.asyncio
async def test_dry_run_execute_returns_preview_lines(igir_test_env, monkeypatch):
    """Dry-run execute endpoint should return clean preview output lines."""
    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
        dat_paths=[f"{igir_test_env['dats_dir']}/set.dat"],
    )

    monkeypatch.setattr(
        igir_routes.igir_service,
        "validate_request",
        Mock(return_value=IgirValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            command_preview="igir clean --clean-dry-run ...",
        )),
    )

    async def _fake_run(_request, cancel_event=None):
        yield {
            "phase": "cleaning",
            "message": "preview",
            "clean_dry_run_results": ["would delete /tmp/a.rom", "would delete /tmp/b.rom"],
        }
        yield {"phase": "done", "message": "done"}

    monkeypatch.setattr(igir_routes.igir_service, "run", _fake_run)

    result = await igir_routes.igir_dry_run_execute(request)
    assert result["valid"] is True
    assert result["count"] == 2
    assert len(result["clean_dry_run_results"]) == 2


@pytest.mark.asyncio
async def test_dry_run_execute_returns_429_on_backpressure(igir_test_env, monkeypatch):
    """Dry-run execute should honor queue backpressure and return 429."""
    from fastapi import HTTPException

    request = IgirJobCreateRequest(
        commands=[IgirCommand.COPY],
        input_paths=[igir_test_env["roms_dir"]],
        output_path=igir_test_env["output_dir"],
        dat_paths=[f"{igir_test_env['dats_dir']}/set.dat"],
    )

    monkeypatch.setattr(
        igir_routes.igir_service,
        "validate_request",
        Mock(return_value=IgirValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            command_preview="igir clean --clean-dry-run ...",
        )),
    )

    async def _raise_backpressure(_request, cancel_event=None):
        raise QueueBackpressureError(
            current_depth=1,
            max_depth=1,
            additional_jobs=1,
        )
        yield {}

    monkeypatch.setattr(
        igir_routes.job_manager,
        "run_igir_preview",
        _raise_backpressure,
    )

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.igir_dry_run_execute(request)

    assert exc_info.value.status_code == 429


# ──────────────── DAT Management ────────────────


@pytest.mark.asyncio
async def test_list_dats_root(igir_test_env, monkeypatch):
    """List DATs at root returns directory listing."""
    from app.models import DatDirectoryListing
    from app.services.igir import igir_service

    monkeypatch.setattr(igir_routes, "igir_service", igir_service)
    result = await igir_routes.list_dats(path=None)
    assert "path" in result
    assert "entries" in result


@pytest.mark.asyncio
async def test_list_dats_traversal_blocked(igir_test_env, monkeypatch):
    """Block path traversal in DAT listing."""
    from fastapi import HTTPException
    from app.services.igir import igir_service

    monkeypatch.setattr(igir_routes, "igir_service", igir_service)

    with pytest.raises(HTTPException) as exc_info:
        await igir_routes.list_dats(path="../../etc")

    assert exc_info.value.status_code == 403


# ──────────────── Version ────────────────


@pytest.mark.asyncio
async def test_get_version(igir_test_env, mock_igir_service):
    """Get igir version endpoint."""
    mock_igir_service.get_version = AsyncMock(return_value="4.3.0")
    monkeypatch_attr = igir_routes.igir_service
    igir_routes.igir_service = mock_igir_service

    try:
        result = await igir_routes.get_igir_version()
        assert result["version"] == "4.3.0"
    finally:
        igir_routes.igir_service = monkeypatch_attr
