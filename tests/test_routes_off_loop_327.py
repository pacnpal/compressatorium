"""No route resolves a path against the Volumes on the event loop (#327).

Volume containment stats and resolves (``realpath``, the bounded root probes),
so on the event loop a mount that stops answering froze every request. Each
route below now runs that work through the one shared helper the Auto routes
use (``routes.volume_guard.off_loop_bounded``): a disposable thread, bounded by
``VOLUME_PROBE_TIMEOUT_SECONDS``, 503 past the bound.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import threading

import pytest

from app.models import (
    CheckDuplicatesRequest,
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
)
from app.routes import convert as convert_routes
from app.routes import files as files_routes
from routes import volume_guard  # the module the routes import (app/ is on sys.path)

_BLOCK_SECONDS = 3.0


def _stuck(release: threading.Event, answer):
    def stuck(*_args, **_kwargs):
        release.wait(_BLOCK_SECONDS)   # a mount that stops answering
        return answer
    return stuck


async def _assert_bounded_and_loop_free(request_coro) -> None:
    """*request_coro* answers 503 at the bound while another request is served."""
    loop = asyncio.get_running_loop()
    request = asyncio.ensure_future(request_coro)
    await asyncio.sleep(0.05)              # the request is in its Volume check
    started = loop.time()
    await convert_routes.list_jobs()       # a trivial request, concurrently
    served_in = loop.time() - started
    with pytest.raises(convert_routes.HTTPException) as exc:
        await asyncio.wait_for(request, 2)
    answered_in = loop.time() - started

    assert served_in < 0.25, served_in        # the loop was not blocked
    assert exc.value.status_code == 503
    assert "Volume" in str(exc.value.detail)
    assert answered_in < 1.5, answered_in      # at the bound, not when the mount wakes


def _create(path: str):
    return convert_routes.create_job(JobCreateRequest(
        file_path=path, mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))


def _delete_plan(path: str):
    return convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[path], mode=ConversionMode.CREATECD,
    ))


def _check_duplicates(path: str):
    return convert_routes.check_duplicates(CheckDuplicatesRequest(
        file_paths=[path], mode=ConversionMode.CREATECD,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call", [_create, _delete_plan, _check_duplicates],
    ids=["create", "delete-plan", "check-duplicates"],
)
async def test_a_stuck_containment_check_answers_503_and_frees_the_loop(
    route_env, monkeypatch, call,
):
    release = threading.Event()
    monkeypatch.setattr(convert_routes, "is_within_configured_volumes", _stuck(release, True))
    monkeypatch.setattr(volume_guard, "VOLUME_PROBE_TIMEOUT_SECONDS", 0.5)
    path = str(route_env["tmp_path"] / "game.iso")
    try:
        await _assert_bounded_and_loop_free(call(path))
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_stuck_same_path_check_in_planning_answers_503(route_env, monkeypatch):
    """``plan_job``'s overwrite-onto-its-own-source check resolves both paths."""
    release = threading.Event()
    image = route_env["tmp_path"] / "game.iso"
    image.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(convert_routes, "_is_same_path", _stuck(release, False))
    monkeypatch.setattr(volume_guard, "VOLUME_PROBE_TIMEOUT_SECONDS", 0.5)
    request = JobCreateRequest(
        file_path=str(image), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.OVERWRITE, delete_on_verify=False,
    )
    try:
        await _assert_bounded_and_loop_free(convert_routes.create_job(request))
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "make"),
    [
        (lambda p: files_routes.list_files(path=p, show_archives=True,
                                           summarize_archives=True), "dir"),
        (lambda p: files_routes.search_files(path=p, recursive=True,
                                             include_archives=True), "dir"),
        (lambda p: files_routes.list_archive(path=p), "zip"),
    ],
    ids=["list", "search", "archive"],
)
async def test_a_stuck_file_browser_check_answers_503_and_frees_the_loop(
    route_env, monkeypatch, call, make,
):
    release = threading.Event()
    target = route_env["tmp_path"] / ("games" if make == "dir" else "set.zip")
    if make == "dir":
        target.mkdir()
    else:
        target.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    monkeypatch.setattr(files_routes, "ensure_path_within_volumes", _stuck(release, target))
    monkeypatch.setattr(volume_guard, "VOLUME_PROBE_TIMEOUT_SECONDS", 0.5)
    try:
        await _assert_bounded_and_loop_free(call(str(target)))
    finally:
        release.set()


@pytest.mark.asyncio
async def test_paths_outside_the_volumes_are_still_refused(route_env) -> None:
    outside = "/definitely/not/a/volume/game.iso"
    with pytest.raises(convert_routes.HTTPException) as exc:
        await _create(outside)
    assert exc.value.status_code == 403
    plan = await _delete_plan(outside)
    assert plan["blocked"] is True
    assert plan["items"][0]["unsafe_paths"] == ["Source path outside configured volumes"]
    assert await _check_duplicates(outside) == []
    with pytest.raises(convert_routes.HTTPException) as exc:
        await files_routes.list_files(
            path="/definitely/not/a/volume", show_archives=True, summarize_archives=True,
        )
    assert exc.value.status_code == 403


# Calls that stat or resolve a path against the Volumes.
_BLOCKING = {
    "is_within_configured_volumes", "ensure_path_within_volumes", "volume_containment",
    "lock_boundary_root", "platform_boundary_root", "realpath", "resolve",
    "source_companions_are_safe", "_is_same_path", "get_volume_name_for_path",
}
# A callable handed to one of these runs off the loop.
_OFFLOADERS = {
    "run_in_threadpool", "run_detached", "bounded_path_check", "off_loop_bounded",
    "to_thread", "run_detached_within_stall_bound", "_probe", "partial",
}


def _name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _on_loop_calls(fn: ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []

    def visit(node, offloaded=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not fn:
            return
        if isinstance(node, ast.Call):
            name = _name(node.func)
            if name in _BLOCKING and not offloaded:
                found.append(f"{fn.name}:{node.lineno} {name}()")
            for child in ast.iter_child_nodes(node):
                visit(child, offloaded or name in _OFFLOADERS)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, offloaded)

    for stmt in fn.body:
        visit(stmt)
    return found


def test_no_route_resolves_a_path_on_the_event_loop() -> None:
    """Every async function in ``app/routes`` hands containment to a thread."""
    routes = pathlib.Path(__file__).resolve().parents[1] / "app" / "routes"
    offenders: list[str] = []
    for path in sorted(routes.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                offenders += [f"{path.name}:{c}" for c in _on_loop_calls(fn)]
    assert offenders == [], offenders
