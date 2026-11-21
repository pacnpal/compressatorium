"""Guard the registry-derived frontend fact lists (issue #186, site 3).

``src/lib/util/fileIcon.js`` used to re-type every game/disc extension, so a
new tool's files silently fell through to the generic File glyph. It now
derives from ``src/lib/tools/registry.js``: fileIcon builds its disc/game
buckets from a small ``TOOL_MEDIA`` map over ``registry.all()``. This test
evaluates those JS modules with Node — the same engine the app uses, so the ext
constants and spreads resolve exactly — and fails if the derived coverage
drifts from the registry: a filterable extension with no icon bucket or an
unclassified tool. (Help no longer lists modes; the restructure moved that to
the README, and ``test_frontend_convert_targets.py`` guards the Convert-to
list's coverage instead.) Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_REGISTRY_JS = _SRC / "lib" / "tools" / "registry.js"
_FILE_ICON_JS = _SRC / "lib" / "util" / "fileIcon.js"
_DAT_MATCHABLE_JS = _SRC / "lib" / "util" / "datMatchable.js"
_FILE_BROWSER_JS = _SRC / "lib" / "stores" / "fileBrowser.svelte.js"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in (os.environ.get("NODE"), "/opt/node22/bin/node", "/usr/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _strip_imports(src: str) -> str:
    """Drop every top-level ``import ...;`` line so the modules can be
    concatenated and evaluated as one plain-Node ESM. The only import with a
    runtime value (registry's ``api``) is stubbed by the caller; every other
    import (Lucide icon components, the intra-module ``registry`` reference) is
    unused by the pure data this dump reads."""
    kept = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") and stripped.endswith(";"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _dump(tmp_path: Path) -> dict:
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend registry modules")

    # registry.js is the source of truth; its only import (`api`) is stubbed.
    registry_src = _REGISTRY_JS.read_text(encoding="utf-8")
    needle = "import { api } from '$lib/api/endpoints.js';"
    assert needle in registry_src, "registry.js import shape changed; update the stub"
    registry_src = "const api = {};\n" + _strip_imports(registry_src)

    # fileIcon.js imports registry (now in scope) + Lucide glyphs (unused here).
    file_icon_src = _strip_imports(_FILE_ICON_JS.read_text(encoding="utf-8"))
    # datMatchable.js is pure; fileIcon.js takes its ARCHIVE_EXTS from it (#427).
    matchable_src = _DAT_MATCHABLE_JS.read_text(encoding="utf-8")

    dump = (
        "\nconst __out = {"
        " filterable: registry.allFilterableExts(),"
        " icon_categories: ICON_EXT_CATEGORIES,"
        " tool_ids: registry.all().map((t) => t.id),"
        " tool_media: Object.keys(TOOL_MEDIA),"
        " info_order: Object.fromEntries(["
        "   'Game.nkit.iso','Game.nkit.gcz','Game.iso','Game.gcz',"
        "   'out.chd','disc.rvz','Game.cso','Game.7z'"
        " ].map((p) => [p, registry.infoToolsForPath(p).map((t) => t.id)])),"
        " display_ext: Object.fromEntries(["
        "   ['Halo 3 (USA)-full.xiso.iso', '.iso', 'file'],"
        "   ['Game.NKIT.ISO', '.iso', 'file'], ['Game.nkit.gcz', '.gcz', 'file'],"
        "   ['Game.iso', '.iso', 'file'], ['nkit.iso', '.iso', 'file'],"
        "   ['out.chd', '.chd', 'file'], ['Game.xiso.iso', '.iso', 'directory'],"
        "   ['Game.zip', '.zip', 'archive'], ['noext', '', 'file']"
        " ].map(([n, e, t]) => [`${t}:${n}`,"
        "   registry.displayExtension({ name: n, extension: e, type: t })])),"
        " content_refined: registry.all().filter((t) => t.contentRefined).map((t) => t.id),"
        " verify_bindings: registry.all().filter((t) => (t.verifyExts ?? []).length)"
        "   .map((t) => [t.id, typeof t.verify, typeof t.verifyBatch]),"
        " info_rows: Object.fromEntries(["
        "   ['dosz', 'Abaron (1995).dosz', ['exodos'], false],"
        "   ['xiso', 'Halo.xiso.iso', ['xiso'], false],"
        "   ['zar', 'Halo.zar', ['rom_converto'], false],"
        "   ['source3ds', 'Game.3ds', [], false],"
        "   ['zip_archive', 'Bundle.zip', [], true],"
        " ].map(([k, p, by, arch]) => [k,"
        "   registry.infoToolForRow(p, by, arch)?.id ?? null])),"
        " content_refined_inputs: registry.all().filter((t) => t.contentRefined)"
        "   .flatMap((t) => t.modes.flatMap((m) => m.inputExtensions ?? [])),"
        " content_claims: Object.fromEntries(["
        "   ['xbox', ['chdman', 'xiso', 'dolphin']], ['psp', ['chdman', 'cso']],"
        "   ['xbox360', ['chdman', 'xiso', 'rom_converto', 'dolphin']],"
        "   ['empty', []], ['missing', undefined]"
        " ].map(([k, by]) => [k, (registry.contentClaimsFor("
        "   by === undefined ? { path: 'g.iso' } : { path: 'g.iso', convertible_by: by }"
        " ) ?? null)?.map((t) => t.id) ?? null])),"
        "};\n"
        "process.stdout.write(JSON.stringify(__out));\n"
    )

    script = tmp_path / "derives_eval.mjs"
    script.write_text(registry_src + "\n" + matchable_src + "\n" + file_icon_src + dump)
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the frontend registry modules via node:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_file_icon_covers_every_registry_extension(tmp_path):
    data = _dump(tmp_path)

    # Every registered tool must be classified, so a new tool forces an icon
    # decision instead of falling through to the generic File glyph.
    assert set(data["tool_ids"]) == set(data["tool_media"]), (
        "fileIcon.js TOOL_MEDIA is out of sync with the registry tools:\n"
        f"  registry-only (unclassified): {sorted(set(data['tool_ids']) - set(data['tool_media']))}\n"
        f"  TOOL_MEDIA-only (stale):      {sorted(set(data['tool_media']) - set(data['tool_ids']))}"
    )

    cats = data["icon_categories"]
    covered = set(cats["disc"]) | set(cats["game"]) | set(cats["archive"]) | {".chd"}
    uncovered = [ext for ext in data["filterable"] if ext not in covered]
    assert not uncovered, (
        "fileIcon.js renders these registry extensions with the generic File "
        f"glyph (add the owning tool to TOOL_MEDIA): {sorted(uncovered)}"
    )


def test_every_filter_option_can_match_a_row(tmp_path):
    """Each dropdown option must be able to select rows, including compound ones.

    ``allFilterableExts()`` is built from ``sourceExts``/``verifyExts``, so it
    offers ``.nkit.iso`` — but the backend records only the trailing
    ``Path.suffix`` (``.iso``) in ``FileEntry.extension``. The store's filter
    therefore has to match the row NAME as a suffix; an equality test against
    ``extension`` silently yields an empty list for every compound option.

    This drives the real ``filteredEntries`` getter under Node against a
    synthetic row per option, so a regression shows up as "this filter shows
    nothing" rather than being invisible until someone tries it.
    """
    node = _find_node()
    if node is None:
        pytest.skip("node not available to evaluate the frontend modules")

    registry_src = _REGISTRY_JS.read_text(encoding="utf-8")
    registry_src = "const api = {};\n" + _strip_imports(registry_src)
    # Lift the getter's body out of the class so it can be exercised without
    # Svelte's rune compiler, keeping the assertion on the SHIPPED source.
    browser_src = _FILE_BROWSER_JS.read_text(encoding="utf-8")
    start = browser_src.index("get filteredEntries() {")
    end = browser_src.index("\n  }", start) + len("\n  }")
    body = browser_src[start:end].replace("get filteredEntries() {", "", 1)
    body = body[: body.rindex("}")]
    body = body.replace("this.sortedEntries", "rows").replace("this.filter", "filter")

    script = tmp_path / "filter_eval.mjs"
    script.write_text(
        registry_src
        + "\nfunction filteredEntries(rows, filter) {" + body + "}\n"
        + "const exts = registry.allFilterableExts();\n"
        # One row per option, named the way the app would see it, with the
        # backend's own `extension` value: the plain trailing suffix.
        + "const rows = exts.map((ext) => ({"
        + "  type: 'file', name: `Game${ext}`, path: `/vol/Game${ext}`,"
        + "  extension: ext.slice(ext.lastIndexOf('.')) }));\n"
        + "const empty = exts.filter((ext) => filteredEntries(rows, ext).length === 0);\n"
        + "process.stdout.write(JSON.stringify({ exts, empty }));\n"
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        pytest.fail(f"Could not evaluate the file-browser filter via node:\n{proc.stderr}")
    data = json.loads(proc.stdout)

    assert data["exts"], "the filter dropdown offered no options at all"
    assert not data["empty"], (
        "these extension-filter options match no row and would render an empty "
        f"list: {sorted(data['empty'])}"
    )


def test_info_tool_order_prefers_the_most_specific_claim(tmp_path):
    """A compound claim must outrank the generic tail — including a verify owner.

    ``FileInfoModal`` keeps the first ``getInfo()`` that returns. Dolphin's
    reader succeeds on both NKit forms (they carry a real GC/Wii disc header,
    and a ``.nkit.gcz`` is a real GCZ), so if it is tried first the modal never
    reaches ``/nkit-info`` and reports the shrunk image as an ordinary disc.
    The ``.gcz`` case is the sharp one: Dolphin owns it as a *verify* extension.
    """
    order = _dump(tmp_path)["info_order"]

    assert order["Game.nkit.iso"][0] == "nkit"
    assert order["Game.nkit.gcz"][0] == "nkit"
    # The generic-tail owners still follow as fallbacks, not replacements.
    assert "dolphin" in order["Game.nkit.gcz"]

    # Single-suffix paths keep their previous ordering: the verify owner leads
    # where one exists, otherwise declared order.
    assert order["out.chd"][0] == "chdman"
    assert order["disc.rvz"][0] == "dolphin"
    assert order["Game.cso"][0] == "cso"
    assert order["Game.7z"][0] == "romz"
    assert order["Game.iso"][0] == "chdman"
    assert order["Game.gcz"][0] == "dolphin"
    # A plain .iso/.gcz must never route Info at nkit.
    assert "nkit" not in order["Game.iso"]
    assert "nkit" not in order["Game.gcz"]


def test_extension_column_shows_the_compound_extension(tmp_path):
    """The Extension column shows ``.xiso.iso`` / ``.nkit.iso``, display only.

    The row's stored ``extension`` stays the trailing suffix (sorting, DAT-match
    gating and icons key off it); ``displayExtension`` only widens what is shown,
    and only to an extension some tool actually declares.
    """
    shown = _dump(tmp_path)["display_ext"]

    assert shown["file:Halo 3 (USA)-full.xiso.iso"] == ".xiso.iso"
    assert shown["file:Game.NKIT.ISO"] == ".nkit.iso"
    assert shown["file:Game.nkit.gcz"] == ".nkit.gcz"
    # Plain names, a bare compound name with no stem, and non-file rows are
    # shown exactly as stored.
    assert shown["file:Game.iso"] == ".iso"
    assert shown["file:nkit.iso"] == ".iso"
    assert shown["file:out.chd"] == ".chd"
    assert shown["directory:Game.xiso.iso"] == ".iso"
    assert shown["archive:Game.zip"] == ".zip"
    assert shown["file:noext"] == ""


def test_content_claims_read_only_the_backend_probe(tmp_path):
    """``contentClaimsFor`` is the one reading of a content-refined claim.

    The selection gate and the Info modal both use it. A row whose
    ``convertible_by`` names a content-refined tool claims it; a row whose list
    leaves it out claims nothing (so Xbox mode can't tick a PSP ``.iso``); and a
    row with no ``convertible_by`` at all is ``None`` -- unknown, not "no" --
    so the selection gate falls back to the extension check.
    """
    dump = _dump(tmp_path)
    claims = dump["content_claims"]

    assert dump["content_refined"] == ["xiso", "rom_converto"]
    assert claims["xbox"] == ["xiso"]
    assert claims["xbox360"] == ["xiso", "rom_converto"]
    assert claims["psp"] == []
    assert claims["empty"] == []
    assert claims["missing"] is None


def test_no_content_refined_tool_takes_an_archive_as_input(tmp_path):
    """An archive row never carries a ``convertible_by`` claim.

    The listing leaves ``convertible_by`` empty on every ``.zip``/``.7z``/
    ``.rar`` row (only members are annotated), so a ``contentRefined`` tool
    whose source is an archive would find no claim on any row and the
    selection gate would refuse every one. Such a tool (the eXoDOS repack,
    whose source is a ``.zip``) relies on the extension gate and ``plan_job``'s
    ``converts_path`` instead.
    """
    inputs = set(_dump(tmp_path)["content_refined_inputs"])

    assert not inputs & {".zip", ".7z", ".rar"}


def test_every_tool_that_claims_a_verify_extension_can_verify(tmp_path):
    """A row whose extension a tool claims for verify gets a Verify action.

    That action calls the tool's ``verify`` (and bulk verify its
    ``verifyBatch``), so a tool with ``verifyExts`` but no binding would offer
    a Verify that throws "tool.verify is not a function".
    """
    bindings = _dump(tmp_path)["verify_bindings"]

    broken = [b for b in bindings if b[1] != "function" or b[2] != "function"]
    assert broken == []


def test_row_info_only_lands_on_a_tool_with_info(tmp_path):
    """Info on a row resolves to a tool that has ``getInfo``, or to nothing.

    The eXoDOS product (``.dosz``) is verifiable but has no Info to show, so
    its row must not offer an Info action that calls an undefined binding.
    """
    rows = _dump(tmp_path)["info_rows"]

    assert rows == {
        "dosz": None,
        "xiso": "xiso",
        # A ZAR: rom-converto's product, described by xenon info.
        "zar": "rom_converto",
        "source3ds": "z3ds",
        "zip_archive": None,
    }
