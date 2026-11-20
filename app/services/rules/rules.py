"""The Rule: its schema, its defaults and the validation every Rule goes through.

A Rule is a standing instruction to convert the Images of one Platform in one
Library: what to convert them to, how to compress them, where to put the
result, when to run, how much to queue at once, and which Images to consider.
Platforms are independent, because a Library is not homogeneous and one global
setting cannot describe it.

A Rule is one of two kinds (#305). A conversion Rule (``CONVERT``) has a
target format. A check Rule (``CHECK``) has none: once a night, inside its
window and on its weekdays, it checks the stalest Images of its Platform until
``check_budget_gb`` is used (see :mod:`services.rules.check`). A Rule's identity
is (Library, Platform, kind), so one Platform can have one of each.

Pure apart from one thing: an output directory is checked against the
configured Volumes when a Rule is saved. Nothing here knows which Library a
Rule belongs to. The few defaults a Library sets come in as ``defaults``.

Nothing here logs (#477). The sanitizer runs on every read of the Rules (each
GET, the editor, startup, every scheduler tick), so a line logged here repeated
once a minute and could not say which Library it meant. What it drops or
refuses is recorded on the Rule instead (``dropped_codecs``,
``dropped_timezone``, ``unsafe_delete_on_verify``, ``invalid_output_dir``,
``invalid_pattern``) and put in words by :func:`rule_changes`; why it refuses a
Rule outright is :func:`refusal`. The sweep logs both, naming the Library and
the Platform: see ``sweep.report_rule_changes``.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable, Iterable, Mapping
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.round_trip import delete_check_level
from services.tools import registry
from services.tools.base import compression_problem, without_unusable_codecs
from utils.path_utils import is_within_configured_volumes

ORDERS = ("name", "size_desc", "size_asc", "id", "newest")
DUPLICATE_ACTIONS = ("skip", "overwrite", "rename")
# The check that must pass before a delete-source job removes its Source (#302).
# `round_trip` compares decoded content (§3.3.12) where the mode supports it;
# `container_only` is the named opt-out the convert routes take as
# `container_only`. First is the default, and the fallback for a value nobody
# can read: the stronger check, never the weaker one.
CHECK_LEVELS = ("round_trip", "container_only")
# 0 = Monday, matching datetime.weekday().
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)

# Bounds applied to every numeric rule field, so a hand-edited preference blob
# cannot produce a sweep that queues the whole library or runs every second.
_BOUNDS = {
    "interval_minutes": (5, 10080),
    "max_per_run": (1, 1000),
    "priority": (-100, 100),
    "min_size_mb": (0, 1024 * 1024),
    "max_size_mb": (0, 1024 * 1024),
    "compression_level": (0, 22),
    "check_budget_gb": (1, 1024 * 1024),
}

# A check Rule (no target format) runs once per window, not on an interval
# (`schedule._is_due`). Its stored interval is fixed at a day, for display and
# for a reader that only knows intervals; the schedule does not read it.
CHECK_INTERVAL_MINUTES = 24 * 60
DEFAULT_CHECK_BUDGET_GB = 50

# The two kinds of Rule. Each kind is stored on its own (``store``), so a
# Platform can have a conversion Rule and a check Rule at once.
CONVERT = "convert"
CHECK = "check"
RULE_KINDS = (CONVERT, CHECK)

# Cap on a user-supplied filter pattern. A pathological regex is the operator's
# own doing, but length is a cheap first guard against the obvious ones.
_MAX_PATTERN = 500


def _clamp(field: str, value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    low, high = _BOUNDS[field]
    return max(low, min(high, out))


def _parse_hhmm(value: Any) -> dt_time | None:
    """Parse ``"HH:MM"``. None (no window) on anything unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        hour, _, minute = value.strip().partition(":")
        return dt_time(hour=int(hour), minute=int(minute or 0))
    except (TypeError, ValueError):
        return None


def _valid_timezone(value: Any) -> tuple[str, str | None]:
    """``(zone, dropped)``: an IANA zone name we can actually load, or ``"UTC"``
    and the name that could not be (#477: recorded, not logged).

    Validated on the way in so a sweep can never fail on a zone the host has no
    data for -- a rule written on a machine with a fuller tzdata than the
    container's would otherwise break the schedule silently.
    """
    if not isinstance(value, str) or not value.strip():
        return "UTC", None
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC", name
    return name, None


def _valid_pattern(value: Any) -> tuple[str | None, bool]:
    """``(pattern, invalid)``. A mistyped regex is refused, never dropped.

    Dropping it silently was the dangerous option: the filter is what keeps a
    rule to a subset, so a rule whose `(USA` never compiled would come back as
    *unfiltered* and the next unattended sweep could queue the whole platform —
    with delete-on-verify attached. The caller disables the rule instead.
    """
    if not isinstance(value, str) or not value.strip():
        return None, False
    pattern = value.strip()
    if len(pattern) > _MAX_PATTERN:
        # Refused, not trimmed. A prefix of a regex is usually still a valid
        # regex, and it means something else: cutting `^(A|B|C)` mid-alternation
        # can leave a pattern that compiles and matches a different set. An
        # *include* that widens queues ROMs the operator excluded; an *exclude*
        # that narrows stops protecting the titles they meant to skip, with
        # delete-on-verify possibly attached. Both are the "helpfully widened"
        # failure this whole validator exists to refuse.
        return None, True
    try:
        re.compile(pattern)
    except re.error:
        return None, True
    if _has_nested_quantifier(pattern) or _has_overlapping_quantifiers(pattern):
        # It backtracks too long.
        return None, True
    return pattern, False


# Quantifiers that can repeat unboundedly. This is the test for what follows a
# GROUP: `(...)?` cannot blow up, because the outer level consumes at most once.
_UNBOUNDED_QUANTIFIERS = "*+{"

# ...but inside a group that *is* repeated unboundedly, `?` counts. Every
# optional atom doubles the ways the engine can split the same input, so
# `(a?){30}a{30}b` explores exponentially many choices against a long run of
# `a` while matching nothing -- and `re` cannot be interrupted, so the sweep
# holds `_sweep_lock` throughout and each detached probe thread is abandoned
# rather than stopped, eating into the process-wide probe capacity that
# unrelated filesystem checks share. Bounded repetition counts as unbounded
# here: `{30}` is thirty levels of doubling.
_BODY_QUANTIFIERS = "*+{?"


def _has_nested_quantifier(pattern: str) -> bool:
    """Whether *pattern* has the shape that backtracks exponentially.

    A quantified group whose body itself repeats -- ``(a+)+``, ``(\\w+\\s?)*``
    -- or whose body is an alternation, ``(a|a)+``. Those are the shapes where
    the engine has exponentially many ways to split the same input, and where
    a filename of a few dozen characters takes longer than the heat death of
    the sweep.

    Read, never run. The obvious way to measure this is to time the pattern
    against an adversarial string, but executing an operator-supplied regex is
    precisely what CodeQL's ``py/regex-injection`` flags -- and rightly: the
    check would then have to survive the very thing it is looking for, and
    timing is load-dependent, so a busy machine could reject a fine pattern.
    Inspecting the source costs nothing and is deterministic.

    Conservative, and deliberately so. It refuses a little more than it must
    (``(USA|Europe)+`` is harmless but rejected), and it is a shape test, not a
    proof. Being wrong here costs one clear message at save time; being wrong
    the other way wedges the scheduler, because ``re`` cannot be interrupted
    and the sweep holds ``_sweep_lock`` while it matches.
    """
    group_starts: list[int] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _skip_class(pattern, i)
            continue
        if char == "(":
            group_starts.append(i + 1)
            i += 1
            continue
        if char == ")":
            body = pattern[group_starts.pop() if group_starts else 0:i]
            following = pattern[i + 1] if i + 1 < length else ""
            if following and following in _UNBOUNDED_QUANTIFIERS and (
                _contains_quantifier(body) or "|" in _strip_atoms(body)
            ):
                return True
            i += 1
            continue
        i += 1
    return False


def _skip_class(pattern: str, i: int) -> int:
    """Index just past the character class starting at *i*."""
    i += 1
    while i < len(pattern) and pattern[i] != "]":
        i += 2 if pattern[i] == "\\" else 1
    return i + 1


def _contains_quantifier(body: str) -> bool:
    """Whether *body* repeats or optionalises anything.

    Escapes and classes are stripped first so a literal ``\\+`` or a ``[+?]``
    class is not read as a quantifier, and so is any leading group prefix --
    the ``?`` in ``(?:...)``, ``(?=...)`` or ``(?P<name>...)`` is syntax, not
    an optional atom, and reading it as one would refuse every non-capturing
    group in the codebase's own filters.
    """
    return any(c in _BODY_QUANTIFIERS for c in _strip_atoms(_strip_group_prefix(body)))


def _strip_group_prefix(body: str) -> str:
    """*body* without the inline prefix that makes a group non-capturing.

    ``(?:``, ``(?=``, ``(?!``, ``(?<=``, ``(?<!``, ``(?P<name>`` and inline
    flag groups all begin with a ``?`` that is part of the group's *syntax*.
    Only what follows is the body whose repetition matters.
    """
    if not body.startswith("?"):
        return body
    rest = body[1:]
    if rest.startswith(("P<", "<")):
        closed = rest.find(">")
        return rest[closed + 1:] if closed != -1 else rest
    # `:`/`=`/`!` end the prefix; inline flags (`aiLmsux`, `-`) precede them.
    i = 0
    while i < len(rest) and rest[i] in "aiLmsux-":
        i += 1
    if i < len(rest) and rest[i] in ":=!":
        i += 1
    return rest[i:]


def _strip_atoms(body: str) -> str:
    """*body* with escape pairs and character classes removed.

    So a literal ``\\+`` or a ``[+|]`` class cannot be mistaken for a
    quantifier or an alternation.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _skip_class(body, i)
            continue
        out.append(char)
        i += 1
    return "".join(out)


# What each escape class can match, for the overlap test below. `None` means
# "anything" -- an unknown escape is assumed to overlap, since guessing narrow
# is the direction that lets a pathological pattern through.
_CLASS_CHARS = {
    "d": set("0123456789"),
    "w": set(string.ascii_letters + string.digits + "_"),
    "s": set(" \t\n\r\f\v"),
}


def _atom_chars(atom: str) -> set[str] | None:
    """The first characters *atom* can match, or None for "anything".

    Only the cases that appear in a filename filter are modelled: a literal, a
    ``\\d``/``\\w``/``\\s`` class and their negations, ``.``, and a simple
    character class. Anything else -- a group, a backreference, a class with
    ranges -- answers None, which the caller reads as "assume it overlaps".
    """
    if not atom:
        return None
    if atom == ".":
        return None
    if atom.startswith("\\") and len(atom) == 2:
        key = atom[1]
        known = _CLASS_CHARS.get(key.lower())
        if known is None:
            # An escaped literal (\. \+ \/) matches exactly itself; a class we
            # do not model (\b, \A) is treated as "anything".
            return {key} if not key.isalpha() else None
        # An uppercase class is the complement, which overlaps almost
        # everything; treat it as unbounded rather than materialise it.
        return known if key.islower() else None
    if atom.startswith("[") and atom.endswith("]"):
        body = atom[1:-1]
        if body.startswith("^") or "-" in body or "\\" in body:
            return None
        return set(body)
    if len(atom) == 1:
        return {atom}
    return None


def _overlaps(first: set[str] | None, second: set[str] | None) -> bool:
    """Whether two atoms can match the same character. Unknown means yes."""
    if first is None or second is None:
        return True
    return bool(first & second)


def _has_overlapping_quantifiers(pattern: str) -> bool:
    """Whether *pattern* repeats the same character in two adjacent places.

    The polynomial sibling of :func:`_has_nested_quantifier`. ``a*a*a*a*b`` has
    no nested group, so the shape test above passes it, yet on a filename that
    is a long run of ``a`` with no ``b`` the engine tries every way to split
    that run between the four stars -- degree-four growth, which at a hundred
    characters is already a hundred million attempts, with the sweep lock held
    and ``re`` uninterruptible.

    Adjacency is what makes it dangerous: ``a*x*`` is harmless because no
    character can go to both, and ``.*/.*`` is fine because the ``/`` between
    them fixes the split. So two unbounded quantifiers count only when they sit
    next to each other *and* their atoms can match the same character. An atom
    this does not model (a group, an exotic escape) counts as overlapping,
    because refusing a working pattern costs one message at save time while
    accepting a pathological one wedges the scheduler.
    """
    previous: set[str] | None = None
    previous_quantified = False
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char in "(|)":
            # A group boundary neither separates nor joins: `(a*)(a*)b`
            # backtracks exactly like `a*a*b`. An alternation does separate --
            # the branches are alternatives, not neighbours.
            if char == "|":
                previous, previous_quantified = None, False
            i += 1
            continue
        if char == "\\" and i + 1 < length:
            atom, i = pattern[i:i + 2], i + 2
        elif char == "[":
            end = _skip_class(pattern, i)
            atom, i = pattern[i:end], end
        else:
            atom, i = char, i + 1
        quantifier = ""
        if i < length and pattern[i] in "*+?{":
            if pattern[i] == "{":
                end = pattern.find("}", i)
                quantifier, i = (pattern[i:end + 1], end + 1) if end != -1 else ("", i)
            else:
                quantifier, i = pattern[i], i + 1
            # A lazy or possessive marker does not change what can match.
            if i < length and pattern[i] in "?+":
                i += 1
        unbounded = bool(quantifier) and (
            quantifier[0] in "*+" or (quantifier.startswith("{") and "," in quantifier)
        )
        chars = _atom_chars(atom)
        if unbounded and previous_quantified and _overlaps(previous, chars):
            return True
        previous, previous_quantified = chars, unbounded
    return False


# The defaults a Library may override (``Library.rule_defaults``), and what
# they are when it does not.
LIBRARY_DEFAULTS = {
    "interval_minutes": 60,
    "max_per_run": 25,
    "verify_after": False,
    "delete_on_verify": False,
}


def default_rule(
    mode: str = "", *, defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A rule with every field at its default, for the UI to render a new row.

    *defaults* is the Library's ``rule_defaults()``. The scheduling and
    verification defaults come from it rather than from constants here, so
    a deployment that sets ``ROMM_AUTO_CONVERT_INTERVAL_MINUTES``,
    ``ROMM_VERIFY_AFTER_CONVERT`` or ``ROMM_DELETE_SOURCE_AFTER_VERIFY``
    actually gets them for its RomM Rules. Documented as first-run defaults,
    they were previously loaded into ``romm_settings`` and then read by nobody.
    """
    cfg = {**LIBRARY_DEFAULTS, **(defaults or {})}
    return {
        "enabled": False,
        "mode": mode,
        "compression": None,
        "compression_level": None,
        "output_dir": None,
        "duplicate_action": "skip",
        "delete_on_verify": bool(cfg["delete_on_verify"]),
        "verify_after": bool(cfg["verify_after"]),
        # Read only when delete_on_verify is on. Kept while it is off, so
        # switching delete-source off and on again keeps the operator's choice.
        "check_level": CHECK_LEVELS[0],
        "split": False,
        # scheduling
        "interval_minutes": _clamp(
            "interval_minutes", cfg["interval_minutes"],
            LIBRARY_DEFAULTS["interval_minutes"],
        ),
        "window_start": None,
        "window_end": None,
        "days": list(ALL_DAYS),
        # IANA zone the window and weekday mask are evaluated in. The editor
        # sends the browser's zone; UTC is the fallback for a rule written
        # before this existed. Stored as a name rather than a fixed offset so
        # the window stays correct across DST.
        "timezone": "UTC",
        # queueing
        "max_per_run": _clamp(
            "max_per_run", cfg["max_per_run"], LIBRARY_DEFAULTS["max_per_run"],
        ),
        "priority": 0,
        "order": "name",
        # selection filters
        "min_size_mb": 0,
        "max_size_mb": 0,
        "include_pattern": None,
        "exclude_pattern": None,
        "only_unmatched": False,
        "only_matched": False,
        # Set only when a submitted output_dir was refused, so the editor can
        # say why the rule is not writing where the operator asked.
        "invalid_output_dir": None,
        # Set when delete-on-verify was refused because this mode + compression
        # cannot verify strongly enough to justify removing the source.
        "unsafe_delete_on_verify": False,
        # The codecs a stored Rule named that its mode can't use (#474),
        # dropped from `compression` so its jobs run on what the mode takes.
        "dropped_codecs": [],
        # A stored timezone the host has no data for (#477), replaced by UTC.
        "dropped_timezone": None,
        # Set when a submitted include/exclude regex would not compile, so the
        # editor can say why the rule is paused instead of silently widening it.
        "invalid_pattern": False,
        "invalid_window": False,
        # A check Rule's nightly byte budget, in GB. Read only when the Rule
        # has no target format (`is_check_rule`).
        "check_budget_gb": DEFAULT_CHECK_BUDGET_GB,
    }


def is_check_rule(rule: Mapping[str, Any]) -> bool:
    """Whether *rule* checks Images rather than converting them: it has no target."""
    return not rule.get("mode")


class UnusableCompression(ValueError):
    """A submitted Rule names a codec (or level) its mode refuses (#474).

    The message is ``compression_problem``'s, the one ``/api/jobs`` answers
    400 with for the same pair.
    """

    def __init__(self, platform_id: str, problem: str) -> None:
        super().__init__(problem)
        self.platform_id = platform_id
        self.problem = problem


def refuse_unusable_compression(raw: Any) -> None:
    """Raise :class:`UnusableCompression` for a submitted Rule whose mode
    refuses its compression, as a job asking for it is refused (#474).

    For submissions, so nothing is saved that is sure to fail per Image. A
    stored Rule is read leniently instead: ``normalize_rule`` drops the codecs
    its mode can't use. Rules ``normalize_rule`` would drop (no mode, or one no
    tool provides) or whose mode takes no compression are left to it.
    """
    if not isinstance(raw, Mapping):
        return
    for platform_id, rule in raw.items():
        if not isinstance(rule, Mapping) or not rule.get("compression"):
            continue
        try:
            spec = registry.spec(rule.get("mode"))
        except (KeyError, TypeError):
            continue
        if not (spec.supports_compression or spec.supports_compression_level):
            continue
        parts = (p.strip() for p in str(rule["compression"]).split(","))
        problem = compression_problem(spec, ",".join(p for p in parts if p))
        if problem:
            raise UnusableCompression(str(platform_id), problem)


def normalize_rule(
    raw: Any, *, kind: str = CONVERT, check_volumes: bool = True,
    volumes_unreachable: bool = False, defaults: Mapping[str, Any] | None = None,
    duplicate_actions: Iterable[str] = DUPLICATE_ACTIONS,
) -> dict[str, Any] | None:
    """Coerce one submitted/stored rule into the full schema.

    *kind* is the Rule's kind. A ``CONVERT`` Rule with no mode, or a mode no
    registered tool provides, cannot be honoured: None. A ``CHECK`` Rule has no
    mode, whatever *raw* says. Tolerant elsewhere: a rule naming a stale option
    falls back to that field's default rather than failing the whole sweep,
    because these blobs outlive the tools that were installed when they were
    written. *defaults* is the Library's ``rule_defaults()``.
    *duplicate_actions* are the policies the Library allows (see
    ``library.duplicate_actions``); any other falls back to ``skip``.
    """
    if not isinstance(raw, dict):
        return None
    if kind == CHECK:
        # The check sweep reads none of a conversion's selection settings, so
        # a check Rule takes none (they keep their defaults): what is saved is
        # what runs, and a stray regex cannot pause it.
        raw = {k: v for k, v in raw.items() if k not in _CONVERSION_ONLY_FIELDS}
    out = default_rule(defaults=defaults)
    mode = raw.get("mode") if kind == CONVERT else None
    if kind == CONVERT and not (isinstance(mode, str) and mode):
        return None
    if refusal(raw, kind=kind) is not None:
        return None
    if isinstance(mode, str) and mode:
        out["mode"] = mode

    spec = registry.spec(out["mode"]) if out["mode"] else None

    out["enabled"] = bool(raw.get("enabled", False))
    # Compression is only meaningful where the mode supports it; storing it
    # otherwise would be silently dropped at submit time anyway.
    # `supports_compression or supports_compression_level`, matching the shared
    # CompressionPicker gate. nsz declares only the latter but still offers a
    # solid/block layout through `compressionCodecs`, so the stricter test
    # dropped the layout AND, because the level rides on the same string, the
    # level with it.
    if spec is not None and (
        spec.supports_compression or spec.supports_compression_level
    ) and raw.get("compression"):
        # Only the codecs the mode takes (#474): a Rule saved before its mode
        # named them (Create DVD with cdzl) would otherwise queue a job per
        # Image that fails in the tool. None left runs the tool's own codecs.
        # A save is refused first (`refuse_unusable_compression`), so this is
        # a stored Rule, and the editor says what was dropped.
        out["compression"], out["dropped_codecs"] = without_unusable_codecs(
            spec, str(raw["compression"]),
        )
    # A level only for a codec that takes one (#363): the Rule's own codec, or
    # the mode's default when blank (WIA on "Tool default" runs none, which
    # takes no level). Dropped, as this sanitizer drops any stale value, and
    # by the same rule the job route refuses with.
    if (
        spec is not None
        and spec.supports_compression_level
        and raw.get("compression_level") is not None
        and not spec.takes_no_level(out["compression"])
    ):
        out["compression_level"] = _clamp("compression_level", raw["compression_level"], 0)
    # A check Rule writes nothing, so its output folder is never read.
    if out["mode"] and raw.get("output_dir") and not check_volumes:
        # Reading back what was already validated when it was saved. The check
        # below resolves the candidate and stats every configured volume, and
        # `get_rules()` is awaited by the rules API and by every sweep while it
        # holds `_sweep_lock` -- so on an unresponsive mount that one stat would
        # take the whole event loop down with it. A blob edited straight in the
        # database bypasses the save-time check, which is why the sweep tests
        # the destination again, off the loop, before it queues anything.
        out["output_dir"] = str(raw["output_dir"])
        if raw.get("invalid_output_dir"):
            out["invalid_output_dir"] = raw["invalid_output_dir"]
            out["enabled"] = False
    elif out["mode"] and raw.get("output_dir"):
        candidate = str(raw["output_dir"])
        within = False if volumes_unreachable else is_within_configured_volumes(candidate)
        # The sweep queues through the job manager directly, so it does not get
        # `/jobs/batch`'s containment check for free. An unvalidated rule could
        # otherwise point the converter at any writable path in the container.
        # Refused here, at the edge where the value is accepted, and again in
        # the sweep before anything is queued.
        if within:
            out["output_dir"] = candidate
        else:
            out["output_dir"] = None
            out["invalid_output_dir"] = candidate
            # Pausing matters: with output_dir cleared the sweep would happily
            # write beside each source instead, filling a filesystem the
            # operator did not choose. The editor surfaces the rejected path.
            out["enabled"] = False
    # Narrowed to what the Library allows. `skip` is the narrow fallback: it
    # never replaces an existing file, so a stored blob naming a policy the
    # Library cannot honour runs as the safe one rather than not at all.
    if raw.get("duplicate_action") in DUPLICATE_ACTIONS and (
        raw["duplicate_action"] in tuple(duplicate_actions)
    ):
        out["duplicate_action"] = raw["duplicate_action"]
    # Each gated on its own capability. They are not the same question:
    # `supports_delete_on_verify` says the *source may be removed* once the
    # check passes, while `mode_supports_verify` says the check exists at all.
    # Asking the delete flag for both put "verify each converted file" out of
    # reach for a mode that can be checked but must never delete -- makeps3iso
    # reads PARAM.SFO back out of the ISO it built, and deleting the curated
    # game folder on that basis is what the mode refuses, not the reading.
    if spec is not None and registry.mode_supports_verify(out["mode"]):
        # `out[...]` as the fallback, not False: an omitted field must inherit
        # the configured default rather than silently overriding it.
        out["verify_after"] = bool(raw.get("verify_after", out["verify_after"]))
    else:
        out["verify_after"] = False
    if spec is not None and spec.supports_delete_on_verify:
        out["delete_on_verify"] = bool(
            raw.get("delete_on_verify", out["delete_on_verify"]),
        )
        # A mode that *can* be verified is not always safely deletable: jwud's
        # verify is a structural WUX walk backed only by JWUDTool's own
        # byte-for-byte pass, which `-noVerify` turns off. The manual route
        # refuses that combination; automation must too, or an unattended rule
        # deletes a 25 GB source on the strength of a geometry check.
        tool = registry.for_mode(out["mode"])
        if out["delete_on_verify"] and not tool.delete_on_verify_is_safe(
            out["mode"], out["compression"],
        ):
            out["delete_on_verify"] = False
            out["unsafe_delete_on_verify"] = True
    else:
        out["delete_on_verify"] = False
    if raw.get("check_level") in CHECK_LEVELS:
        out["check_level"] = raw["check_level"]
    out["split"] = bool(raw.get("split", False))

    # `out[...]` as the fallback, not a literal: `default_rule()` already seeded
    # these from the effective settings, and re-clamping against 60/25 threw that
    # away for any stored or API-submitted rule that simply omits the field.
    out["interval_minutes"] = _clamp(
        "interval_minutes", raw.get("interval_minutes"), out["interval_minutes"],
    )
    out["window_start"] = raw.get("window_start") or None
    out["window_end"] = raw.get("window_end") or None
    # Half a window is not a window. One endpoint filled in (or one that does
    # not parse) used to mean "no time restriction", so a rule an operator was
    # narrowing to 22:00-04:00 ran all day on its selected weekdays the moment
    # they typed the first field -- unattended, delete-on-verify included.
    # Pause and say so, the same as an output folder outside the volumes.
    starts, ends = _parse_hhmm(out["window_start"]), _parse_hhmm(out["window_end"])
    if (out["window_start"] or out["window_end"]) and (starts is None or ends is None):
        out["invalid_window"] = True
        out["enabled"] = False
    elif kind == CONVERT and starts is not None and starts == ends:
        # The same start and end is a slip for a conversion: read literally it
        # opens for one instant, read as "all day" it converts through working
        # hours, delete-source included. Paused, as half a window is. (A check
        # Rule reads it as the whole day: `schedule._window`.)
        out["invalid_window"] = True
        out["enabled"] = False
    out["timezone"], out["dropped_timezone"] = _valid_timezone(raw.get("timezone"))
    days = raw.get("days")
    if isinstance(days, list):
        parsed = sorted({int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
        # An empty list is a choice, not a missing value: deselecting every
        # weekday used to come back as *all* of them, so a rule the operator
        # had deliberately parked -- delete-on-verify and all -- ran every day
        # instead of none. Omitting the field still inherits the default (all
        # days); sending `[]` means "no scheduled days", and the editor says
        # the rule will only run when you press Run now.
        out["days"] = parsed

    out["max_per_run"] = _clamp(
        "max_per_run", raw.get("max_per_run"), out["max_per_run"],
    )
    out["priority"] = _clamp("priority", raw.get("priority"), 0)
    if raw.get("order") in ORDERS:
        out["order"] = raw["order"]

    out["min_size_mb"] = _clamp("min_size_mb", raw.get("min_size_mb"), 0)
    out["max_size_mb"] = _clamp("max_size_mb", raw.get("max_size_mb"), 0)
    out["include_pattern"], bad_include = _valid_pattern(raw.get("include_pattern"))
    out["exclude_pattern"], bad_exclude = _valid_pattern(raw.get("exclude_pattern"))
    if bad_include or bad_exclude:
        # Pause rather than run wider than asked. The editor shows why.
        out["invalid_pattern"] = True
        out["enabled"] = False
    # A refused pattern is replaced by the wider fallback, so the *reason* has to
    # survive the round trip or the pause becomes unreadable: re-normalizing a
    # stored rule sees `exclude_pattern: None`, finds nothing wrong with it, and
    # clears the flag -- leaving the editor showing a rule paused for no stated
    # reason, and the sweep unable to tell an operator's pause from this one.
    if raw.get("invalid_pattern"):
        out["invalid_pattern"] = True
        out["enabled"] = False
    # `invalid_window` is never read from `raw`. The window is kept as sent, so
    # the check above recomputes the flag on every save; honouring a sent flag
    # would keep a corrected window paused for good.

    out["check_budget_gb"] = _clamp(
        "check_budget_gb", raw.get("check_budget_gb"), DEFAULT_CHECK_BUDGET_GB,
    )
    if is_check_rule(out):
        # Once per window (schedule._is_due); the interval is only a label.
        out["interval_minutes"] = CHECK_INTERVAL_MINUTES

    out["only_unmatched"] = bool(raw.get("only_unmatched", False))
    out["only_matched"] = bool(raw.get("only_matched", False))
    # Mutually exclusive; "both" is the same as neither, and silently meaning
    # "nothing matches" would be a confusing way to spend a sweep.
    if out["only_matched"] and out["only_unmatched"]:
        out["only_matched"] = out["only_unmatched"] = False
    return out


def refusal(raw: Any, *, kind: str = CONVERT) -> str | None:
    """Why :func:`normalize_rule` refuses *raw* outright, in words for a log
    line, or None.

    A conversion Rule naming a mode no registered tool provides, or one that
    can't run unattended, is refused: no sweep can run it. Refused here as well
    as omitted from the editor, because a rules blob can be written straight
    into the database. A mode that turns its own product into a differently
    named file never terminates under automation -- see
    ``registry.mode_is_automatable``. None for anything else, a Rule with no
    mode included: that is an empty row, with nothing to report.
    """
    if kind != CONVERT or not isinstance(raw, Mapping):
        return None
    mode = raw.get("mode")
    if not (isinstance(mode, str) and mode):
        return None
    try:
        registry.spec(mode)
    except KeyError:
        return f"it names unknown mode {mode!r}"
    if not registry.mode_is_automatable(mode):
        return f"its mode {mode!r} can't run unattended"
    return None


def rule_changes(rule: Mapping[str, Any]) -> list[str]:
    """What :func:`normalize_rule` dropped or refused on *rule*, in words for a
    log line (#477). Empty when it took the Rule as stored.

    Read off the fields the sanitizer records, so the sweep can say it once
    where it runs the Rule, rather than the sanitizer on every read.
    """
    changes: list[str] = []
    if rule.get("dropped_codecs"):
        try:
            label = registry.spec(rule.get("mode")).label
        except (KeyError, TypeError):
            label = str(rule.get("mode"))
        kept = rule.get("compression") or "the tool's own codecs"
        changes.append(
            f"{label} can't use {', '.join(rule['dropped_codecs'])} compression; "
            f"running it on {kept}",
        )
    if rule.get("unsafe_delete_on_verify"):
        changes.append(
            f"delete-on-verify refused for {rule.get('mode')} with compression "
            f"{rule.get('compression')!r}; its Sources are kept",
        )
    if rule.get("dropped_timezone"):
        changes.append(
            f"unknown timezone {rule['dropped_timezone']!r}; its window runs on UTC",
        )
    if rule.get("invalid_output_dir"):
        changes.append(
            f"output folder {rule['invalid_output_dir']!r} refused; the Rule is paused",
        )
    if rule.get("invalid_pattern"):
        changes.append("a filter pattern was refused; the Rule is paused")
    return changes


# What a check Rule never uses: its order is stalest first, its limit the budget.
_CONVERSION_ONLY_FIELDS = (
    "duplicate_action", "check_level", "max_per_run", "order", "min_size_mb",
    "max_size_mb", "include_pattern", "exclude_pattern", "only_unmatched",
    "only_matched", "invalid_pattern",
)


def takes_container_only(rule: Mapping[str, Any]) -> bool:
    """Whether jobs this Rule queues take the convert routes' ``container_only`` opt-out.

    Only a Rule that deletes its Source has a check to opt out of; a Rule that
    keeps it reads nothing extra whatever its level says.
    """
    return bool(rule.get("delete_on_verify")) and rule.get("check_level") == "container_only"


def effective_check_level(rule: Mapping[str, Any]) -> str | None:
    """The check a delete-source job from this Rule will pass before deleting.

    What Preview shows, as opposed to the stored preference: ``recognized``
    for a mode whose proof is Recognized (#423, whatever the stored level),
    ``round_trip`` where the mode can Round-trip and the Rule did not opt out,
    otherwise ``container_only``. None when the Rule keeps its Source. Decided
    by ``delete_check_level``, the same helper the job path asks.
    """
    return delete_check_level(
        rule.get("mode") or "",
        delete_on_verify=bool(rule.get("delete_on_verify")),
        container_only=takes_container_only(rule),
    )


def normalize_rules(
    raw: Any, *, check_volumes: bool = True, volumes_unreachable: bool = False,
    platform_id: Callable[[Any], str | None],
    defaults: Mapping[str, Any] | None = None,
    duplicate_actions: Iterable[str] = DUPLICATE_ACTIONS,
    kind: str = CONVERT,
) -> dict[str, dict]:
    """Normalize a whole rule set of one *kind*, keyed by Platform id.

    *platform_id* is the Library's ``canonical_platform_id``: a key it does
    not accept is dropped, and the rest are stored in its canonical form.
    *defaults* is the Library's ``rule_defaults()``.

    *volumes_unreachable* is the third answer to "is this output directory
    inside the volumes": not yes, not "trust what was saved", but *nobody
    could tell*. Every such rule is paused with its path recorded, because a
    rule that cannot be honoured is paused here, never widened -- and an
    output directory nobody can resolve is one the conversion could not have
    written to anyway.
    """
    if not isinstance(raw, dict):
        return {}
    rules: dict[str, dict] = {}
    for key, value in raw.items():
        canonical = platform_id(key)
        if canonical is None:
            continue
        rule = normalize_rule(
            value,
            kind=kind,
            check_volumes=check_volumes,
            volumes_unreachable=volumes_unreachable,
            defaults=defaults,
            duplicate_actions=duplicate_actions,
        )
        if rule is not None:
            rules[canonical] = rule
    return rules


# The rule fields that decide *what* a conversion produces. Change any of
# them and the outputs recorded against this platform no longer answer the
# question "has this rule converted that source" -- so the provenance has to
# go with them.
OUTPUT_IDENTITY_FIELDS = (
    "mode",
    "output_dir",
    "compression",
    "compression_level",
    "split",
    "duplicate_action",
)


def _output_identity(rule: dict | None) -> tuple:
    if not rule:
        return ()
    return tuple(rule.get(field) for field in OUTPUT_IDENTITY_FIELDS)


# What `normalize_rule` sets when it refuses a value. Each one has already
# replaced that value with a *wider* fallback, which is why a rule carrying any
# of them must not run -- see the sweep's `enabled` gate.
_VALIDATION_FLAGS = ("invalid_output_dir", "invalid_window", "invalid_pattern")


def _validation_paused(rule: dict) -> bool:
    """Was this rule disabled by validation rather than by the operator?"""
    return any(rule.get(flag) for flag in _VALIDATION_FLAGS)
