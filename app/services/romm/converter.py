"""Beside RomM's own converter (#385): RomM's targets in this app's terms.

RomM can convert its own library: with ``ROM_CONVERTO_ENABLED`` on, its
"Convert library" task stores each Platform listed in ``converto.platform_formats``
in that format and deletes the sources after checking nothing but an exit
code. :meth:`RommClient.converter_config` reads that config; this module maps
RomM's target names onto the modes this app has, so the RomM view can flag a
Rule whose Platform RomM also converts, mark the Platforms RomM cannot convert,
and import RomM's targets as Rules. It warns and imports; it never blocks.

Registry-driven: a mode matches a RomM target when it may run under a Rule on
that Platform (:meth:`ToolRegistry.modes_for_platform`, automatable, not an
unpack) and writes one of the extensions RomM writes for that target.
"""

from __future__ import annotations

from typing import Any

from services.tools import registry
from services.tools.spec import ModeKind

# The extensions RomM's rom-converto writes for each library target, from
# ``OPERATIONS`` in RomM's ``backend/adapters/services/rom_converto.py``
# (eb10d95). A target's name is not always its extension: ``z3ds`` writes
# ``.zcia`` / ``.zcci`` / ``.zcxi``. A target added in a later RomM falls back
# to its own name as the extension (:func:`target_extensions`).
ROMM_TARGET_EXTS: dict[str, frozenset[str]] = {
    "z3ds": frozenset({".zcia", ".zcci", ".zcxi"}),
    "cia": frozenset({".cia"}),
    "cci": frozenset({".cci"}),
    "iso": frozenset({".iso"}),
    "cso": frozenset({".cso"}),
    "zso": frozenset({".zso"}),
    "chd": frozenset({".chd"}),
    "rvz": frozenset({".rvz"}),
    "wbfs": frozenset({".wbfs"}),
    "nsz": frozenset({".nsz"}),
    "xcz": frozenset({".xcz"}),
    "nsp": frozenset({".nsp"}),
    "xci": frozenset({".xci"}),
    "zar": frozenset({".zar"}),
}


def target_extensions(target: str) -> frozenset[str]:
    """The extensions RomM writes for *target*."""
    key = (target or "").strip().lower()
    return ROMM_TARGET_EXTS.get(key, frozenset({f".{key}"}) if key else frozenset())


def modes_for_target(slug: str, target: str) -> list[str]:
    """This app's modes that store a *slug* Platform the way RomM's *target* does.

    In registry order, so the first is the one an import picks. Only modes a
    Rule can run: allowed on the Platform, automatable, and not an unpack (the
    Rule editor offers no extract mode). Empty when nothing here writes it.
    """
    exts = target_extensions(target)
    if not exts:
        return []
    return [
        mode for mode in registry.modes_for_platform(slug)
        if registry.spec(mode).kind != ModeKind.EXTRACT
        and registry.mode_is_automatable(mode)
        and registry.mode_output_extensions(mode) & exts
    ]


def describe(converter: dict[str, Any] | None, error: str | None = None) -> dict | None:
    """The ``converter`` block of ``GET /api/romm/status``.

    ``None`` for a RomM without a converter, so the view shows no banner and
    no flags. Otherwise RomM's own answer, plus ``modes``: for each Platform
    slug RomM converts, this app's modes for the same target (empty when there
    is none), which the Rule flags and the import read.
    """
    if converter is None:
        return None
    formats = dict(converter.get("platform_formats") or {})
    return {
        "enabled": bool(converter.get("enabled")),
        "platform_formats": formats,
        "library_targets": converter.get("library_targets"),
        "modes": {slug: modes_for_target(slug, target) for slug, target in formats.items()},
        "error": error,
    }


def import_plan(
    converter: dict[str, Any] | None, platforms: list[dict], rules: dict[str, Any],
) -> dict[str, Any]:
    """The Rules an import of RomM's targets would create, and what it leaves.

    Only Platforms with no conversion Rule yet (*rules* is keyed by Platform
    id), so an import never overwrites one. ``rules`` maps each Platform id to
    the mode picked and RomM's target; ``skipped`` lists the Platforms whose
    RomM target nothing here writes; ``existing`` the Platforms RomM converts
    that already have a Rule. Empty while RomM's converter is off.
    """
    plan: dict[str, Any] = {
        "enabled": bool(converter and converter.get("enabled")),
        "rules": {}, "skipped": [], "existing": [],
    }
    if not plan["enabled"]:
        return plan
    formats = converter.get("platform_formats") or {}
    for platform in sorted(platforms, key=lambda p: str(p.get("id")).rjust(20)):
        slug = str(platform.get("slug") or "").strip().lower()
        target = formats.get(slug)
        if platform.get("id") is None or not target:
            continue
        key = str(platform["id"])
        if key in rules:
            plan["existing"].append(key)
            continue
        modes = modes_for_target(slug, target)
        if not modes:
            plan["skipped"].append({
                "platform_id": key,
                "name": platform.get("display_name") or platform.get("name") or slug,
                "slug": slug,
                "target": target,
            })
            continue
        plan["rules"][key] = {"mode": modes[0], "target": target}
    return plan
