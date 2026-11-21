"""Formats and File info come from the tool registry (#539, plan items 22, 27).

* ``GET /api/tools`` serves every tool's extensions and every mode's every
  ``ModeSpec`` field (``ModeSpec.as_json``), and ``registry.js`` is built from
  it (``registry.load``) instead of keeping a hand-copied mirror. The payload
  here is the route's own answer, called in-test, and the Node checks load
  ``registry.js`` from it the way App.svelte does at start.
* One ``GET /api/info/{tool_id}`` answers for every tool, from the plugin's
  ``info_extensions`` / ``describe`` / ``info_file_errors``; the old per-tool
  URLs are aliases of it, and the verify routes are registered in one loop
  over the registry.
* The per-tool branches in the convert route and the job manager read the
  spec (``no_compression_reason``, ``single_codec``, ``bad_input_skip``) or
  the plugin (``plan_problem``, z3ds's ``output_path``).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from services.tools import registry
from services.tools.spec import ModeSpec

from app.routes import convert as convert_routes
from app.routes import info as info_routes

from .test_frontend_registry_derives_186 import _find_node, loaded_registry_src


@pytest_asyncio.fixture(name="payload")
async def _payload():
    return await info_routes.list_tools()


# ─── The payload ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_modespec_field_is_in_every_mode(payload):
    names = {f.name for f in fields(ModeSpec)}
    modes = [m for tool in payload["tools"] for m in tool["modes"]]
    assert {m["mode"] for m in modes} == {s.mode for s in registry.mode_specs()}
    for mode in modes:
        assert names <= set(mode), f"{mode['mode']} lacks {sorted(names - set(mode))}"
    # The availability report the sidebar reads stays as it was.
    assert {"available", "unavailable"} <= set(payload)


@pytest.mark.asyncio
async def test_fields_are_json_shaped(payload):
    by_mode = {m["mode"]: m for tool in payload["tools"] for m in tool["modes"]}
    rvz = by_mode["dolphin_rvz"]
    assert rvz["kind"] == "compress"
    assert rvz["codecs"] == sorted(registry.spec("dolphin_rvz").codecs)
    assert {"codec": "zstd", "min": 1, "max": 22, "default": 19} in rvz["codec_levels"]
    assert by_mode["folder_to_iso"]["input_kinds"] == ["directory"]
    assert by_mode["pbp_to_iso"]["delete_proof"] == "recognized"
    assert [s["tool_id"] for s in by_mode["cso_to_chd"]["steps"]] == ["cso", "chdman"]
    # Resolved by the registry, not the raw (empty = every input) field.
    assert by_mode["createdvd"]["reads_extensions"] == sorted(
        registry.mode_reads_extensions("createdvd"),
    )
    json.dumps(payload)  # nothing a browser can't parse


@pytest.mark.asyncio
async def test_tool_rows_carry_the_backend_extensions(payload):
    for row in payload["tools"]:
        tool = registry.get(row["id"])
        assert row["input_extensions"] == sorted(tool.input_extensions)
        assert row["verify_extensions"] == sorted(tool.verify_extensions)
        assert row["disc_extensions"] == sorted(tool.disc_extensions)
        assert row["default_compression"] == tool.default_compression


# ─── registry.load(payload), under Node ───────────────────────────────────


def _node(tmp_path, body: str, src: str | None = None):
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate registry.js")
    script = tmp_path / "registry_load.mjs"
    script.write_text((src or loaded_registry_src()) + body, encoding="utf-8")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_ANSWERS = """
const out = {};
for (const t of registry.all()) for (const m of t.modes) {
  const codecs = registry.codecsForMode(m.mode);
  out[m.mode] = {
    spec: registry.specFor(m.mode),
    tool: registry.toolForMode(m.mode).id,
    codecs: codecs.map((c) => c.value),
    levels: Object.fromEntries(codecs.map((c) => [c.value, registry.levelRangeFor(m.mode, c.value)])),
    dc: registry.defaultCompressionForMode(m.mode) ?? null,
    sources: Object.fromEntries(m.inputExtensions.map((e) => [e, [
      ...registry.toolsForSourcePath(`Game${e}`).map((x) => x.id),
      registry.toolForVerifyPath(`Game${e}`)?.id]])),
    shown: Object.fromEntries(m.inputExtensions.map((e) => [e, registry.displayExtension(
      { name: `Game${e}`, extension: e.slice(e.lastIndexOf('.')), type: 'file' })])),
  };
}
process.stdout.write(JSON.stringify({ modes: out, loaded: registry.loaded }));
"""


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def test_registry_answers_every_mode_from_the_backend(tmp_path):
    answers = _node(tmp_path, _ANSWERS)
    assert answers["loaded"] is True
    modes = answers["modes"]
    assert set(modes) == {s.mode for s in registry.mode_specs()}
    wires = {m["mode"]: m for t in info_routes.tool_catalog() for m in t["modes"]}
    for spec in registry.mode_specs():
        got = modes[spec.mode]
        wire = wires[spec.mode]
        # specFor: every ModeSpec field, camelCased, with the backend's value.
        for name, value in wire.items():
            if name == "default_compression":
                continue  # an array on the frontend, checked below
            assert got["spec"][_camel(name)] == value, (spec.mode, name)
        # The UI lists a chain mode under the tool its first step runs.
        steps = getattr(spec, "steps", ())
        assert got["tool"] == (steps[0].tool_id if steps else spec.tool_id)
        # codecsForMode: the mode's own codecs when it names them.
        if spec.codecs:
            assert sorted(got["codecs"]) == sorted(spec.codecs)
        # levelRangeFor: the backend's range for each codec that has one.
        if spec.supports_compression_level and spec.codec_levels:
            for codec in spec.codecs:
                rng = spec.level_range(codec)
                expected = None if rng is None else dict(zip(("min", "max", "default"), rng))
                assert got["levels"][codec] == expected, (spec.mode, codec)
        if spec.default_compression:
            assert got["dc"] == [spec.default_compression]
        # toolsForSourcePath: the tool that runs the mode claims each input (a
        # .chd, chdman's extract/copy input, by its verify claim).
        for ext, owners in got["sources"].items():
            assert got["tool"] in owners or spec.tool_id in owners, (spec.mode, ext)
        # displayExtension: a compound input shows whole (`.nkit.iso`).
        for ext, shown in got["shown"].items():
            assert shown == ext, (spec.mode, ext)


def test_a_tool_the_ui_has_never_heard_of_still_appears(tmp_path):
    """No frontend edit for a new tool: it is listed with plain defaults,
    its modes are selectable and its Info goes through /api/info/{tool}."""
    src = loaded_registry_src()
    extra = {
        "id": "newtool", "label": "New Tool",
        "input_extensions": [".new"], "output_extensions": [".nw"],
        "verify_extensions": [".nw"], "disc_extensions": [],
        "default_compression": None,
        "modes": [{
            "mode": "new_compress", "tool_id": "newtool", "kind": "compress",
            "label": "Compress NEW", "group": "newtool", "output_ext": ".nw",
            "input_extensions": [".new"], "codecs": [], "codec_levels": [],
            "default_compression": None,
        }],
    }
    body = f"""
registry.load({{ tools: [{json.dumps(extra)}] }});
const t = registry.forTool('newtool');
process.stdout.write(JSON.stringify({{
  label: t.label, prefix: t.verifyPrefix, hasInfo: typeof t.getInfo,
  mode: registry.toolForMode('new_compress')?.id,
  sources: registry.toolsForSourcePath('x.new').map((x) => x.id),
  verify: registry.toolForVerifyPath('x.nw')?.id ?? null,
  product: t.productPath('x.new'),
}}));
"""
    got = _node(tmp_path, body, src)
    assert got == {
        "label": "New Tool", "prefix": "newtool", "hasInfo": "function",
        "mode": "newtool", "sources": ["newtool"], "verify": "newtool",
        "product": "x.new",
    }


def test_the_registry_is_empty_until_loaded(tmp_path):
    head, _, tail = loaded_registry_src().rpartition("registry.load(")
    src = head + "void (" + tail
    got = _node(tmp_path, "\nprocess.stdout.write(JSON.stringify("
                "[registry.loaded, registry.all().length, registry.specFor('copy') ?? null]));\n",
                src)
    assert got == [False, 0, None]


def test_a_reply_that_names_no_tools_never_empties_a_loaded_registry(tmp_path):
    """A stale or broken /api/tools answer (no `tools`, an empty list, an error
    body) must not wipe what is loaded: `load` says False and keeps it."""
    got = _node(tmp_path, """
const results = [registry.load({}), registry.load({ tools: [] }), registry.load(null)];
process.stdout.write(JSON.stringify([results, registry.loaded, registry.specFor('copy')?.mode]));
""")
    assert got == [[False, False, False], True, "copy"]


def test_app_retries_the_tools_until_they_load_and_stops_when_unmounted():
    """App loads the registry once: a failure says so and retries with a
    growing delay, a reply after unmount is ignored, and no retry is left
    scheduled after unmount."""
    app = (Path(__file__).resolve().parents[1] / "src" / "App.svelte").read_text("utf-8")
    start = app.index("const loadTools")
    body = app[start:app.index("loadTools();", start)]
    assert "if (disposed) return;" in body
    assert "if (!registry.load(t))" in body
    assert "toolsError = " in body
    assert "setTimeout(() => loadTools(Math.min(delay * 2, 10000)), delay)" in body
    assert "disposed = true;" in app and "clearTimeout(toolsRetry);" in app
    # The waiting state says what is wrong once a fetch failed.
    assert "{toolsError || " in app


# ─── One registry-driven info route ───────────────────────────────────────


@pytest.fixture(name="volume")
def _volume(tmp_path, monkeypatch):
    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_unknown_tool_is_a_404(volume):
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_tool_info("nope", path=str(volume / "a.iso"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_extension_gate_is_the_plugins(volume):
    other = volume / "notes.txt"
    other.write_text("x")
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_tool_info("chdman", path=str(other))
    assert exc.value.status_code == 400
    assert ".chd" in exc.value.detail


@pytest.mark.asyncio
async def test_a_file_that_is_not_what_its_name_says_is_a_422(volume, monkeypatch):
    tool = registry.get("rom_converto")

    async def refuse(_path):
        raise tool.info_file_errors[0]("not a ZAR")

    monkeypatch.setattr(tool, "info", refuse)
    monkeypatch.setattr(info_routes, "_require_info_ready", lambda _id: _ready())
    zar = volume / "Halo.zar"
    zar.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        await info_routes.get_tool_info("rom_converto", path=str(zar))
    assert exc.value.status_code == 422


async def _ready():
    return None


@pytest.mark.asyncio
async def test_old_urls_are_aliases_of_the_one_route(volume, monkeypatch):
    calls = []

    async def describe(tool, path):
        calls.append((tool.id, path))
        return {"ok": tool.id}

    monkeypatch.setattr(info_routes, "_describe", describe)
    for tool_id, (url, name) in info_routes._LEGACY_INFO_ROUTES.items():
        assert await getattr(info_routes, name)(path="/p") == {"ok": tool_id}
        assert await info_routes.get_tool_info(tool_id, path="/p") == {"ok": tool_id}
    paths = {r.path for r in info_routes.router.routes}
    assert {f"/{url}" for url, _ in info_routes._LEGACY_INFO_ROUTES.values()} <= paths
    assert "/info/{tool_id}" in paths
    assert len(calls) == 2 * len(info_routes._LEGACY_INFO_ROUTES)


def test_every_tool_that_verifies_has_its_verify_routes():
    paths = {r.path for r in info_routes.router.routes}
    for tool in registry.all():
        if not tool.verify_extensions:
            continue
        cfg = info_routes._verify_config(tool)
        for suffix in ("verify", "verify/events", "verify-batch/events"):
            assert f"/{cfg.url_prefix}{suffix}" in paths, (tool.id, suffix)
        assert callable(getattr(info_routes, cfg.sync_name))


def test_a_tool_without_a_legacy_verify_entry_gets_the_default():
    cfg = info_routes._default_verify_config(registry.get("rom_converto"))
    assert cfg.url_prefix == "rom-converto-"
    assert cfg.sync_name == "verify_rom_converto"
    assert ".zar" in cfg.bad_ext_detail


# ─── The per-tool branches, now on the spec and the plugin ────────────────


@pytest.mark.parametrize(("mode", "compression", "words"), [
    ("extractcd", "zlib", "only supported for CHD creation/copy"),
    ("dolphin_gcz", "zstd", "GCZ uses fixed internal compression"),
    ("dolphin_iso", "zstd", "not applicable for ISO extraction"),
    ("dolphin_rvz", "zstd,lzma", "one codec at a time"),
    ("createcd", "zlib:5", "takes no compression level"),
])
def test_compression_refusals_read_the_spec(mode, compression, words):
    with pytest.raises(HTTPException) as exc:
        convert_routes._validate_request_compression(registry.spec(mode), mode, compression)
    assert exc.value.status_code == 400
    assert words in exc.value.detail


def test_a_mode_without_a_reason_still_takes_what_it_ignores():
    # The chain drops a stale preset rather than refusing it.
    convert_routes._validate_request_compression(registry.spec("cso_to_chd"), "cso_to_chd", "max")


def test_romz_extract_refuses_an_archive_without_one_rom(tmp_path):
    archive = tmp_path / "Two.zip"
    archive.write_bytes(b"not a zip")
    romz = registry.get("romz")
    assert romz.plan_problem(str(archive), "romz_extract") == "romz_invalid_archive"
    assert romz.plan_problem(str(tmp_path / "Game.gba"), "romz_7z") is None
    assert registry.get("chdman").plan_problem("x.chd", "extractcd") is None


def test_z3ds_output_path_refuses_what_its_mode_does_not_take():
    z3ds = registry.get("z3ds")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        z3ds.output_path("z3ds_compress", "/v/Game.txt")
    assert z3ds.output_path("z3ds_compress", "/v/Game.3ds").endswith("Game.z3ds")


def test_a_tool_with_a_codec_and_level_mode_names_its_default_codec():
    """A Rule may set a level with the codec left on "tool default": the level
    rides on the codec string, so ``":19"`` would reach dolphin-tool as
    ``-c "" -l 19``. Such a tool must name the codec that resolves it, which is
    also what the manual picker seeds (``defaultCompression``, from here)."""
    missing = [
        tool.id for tool in registry.all()
        if any(m.supports_compression and m.supports_compression_level for m in tool.modes)
        and not tool.default_compression
    ]
    assert missing == []
