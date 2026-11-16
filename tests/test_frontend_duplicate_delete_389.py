"""Guards for the duplicate delete's UI (#389).

The Findings store's delete flow is evaluated with Node, as
``test_frontend_findings_view`` does; the wiring is asserted on the source.
"""
from __future__ import annotations

from pathlib import Path

from .test_frontend_findings_view import _run

_SRC = Path(__file__).resolve().parents[1] / "src"
_VIEW = _SRC / "lib" / "components" / "views" / "FindingsView.svelte"
_MODAL = _SRC / "lib" / "components" / "modals" / "DuplicateDeleteModal.svelte"
_ENDPOINTS = _SRC / "lib" / "api" / "endpoints.js"

_ROW = """{ id: 7, kind: 'duplicate', library_id: 'folder:1', fingerprint: 'f7',
  image_key: 'raw:aa', keeper: '/v/Game.iso', keeper_reason: 'the smallest copy',
  deletable: true,
  cause: { copies: [{ path: '/v/Game.iso' }, { path: '/v/Copy.iso' }] } }"""


def test_the_plan_is_asked_for_with_the_group_as_shown(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'] }}], blocked: false, total_delete_count: 1 }};
await findings.openCleanup({_ROW});
process.stdout.write(JSON.stringify({{
  call: __log.find((c) => c[0] === 'duplicateDeletePlan'),
  keep: findings.cleanup.keep, total: findings.cleanup.plan.total_delete_count,
}}));
""")
    # The suggested keeper by default, and the fingerprint the user saw.
    assert data["call"] == ["duplicateDeletePlan", 7, "f7", "/v/Game.iso"], data
    assert data["keep"] == "/v/Game.iso" and data["total"] == 1, data


def test_confirm_deletes_exactly_what_the_plan_listed(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.listFindings = {{ findings: [], dismissed: 0 }};
__answers.duplicateDeletePlan = {{ items: [
  {{ source_path: '/v/Copy.iso', delete_paths: ['/v/Copy.iso'] }},
  {{ source_path: '/v/Disc.cue', delete_paths: ['/v/Disc.cue', '/v/Disc.bin'] }},
], blocked: false, total_delete_count: 3 }};
__answers.deleteDuplicates = {{ results: [] }};
await findings.openCleanup({_ROW});
await findings.confirmCleanup();
process.stdout.write(JSON.stringify({{
  call: __log.find((c) => c[0] === 'deleteDuplicates'),
  open: findings.cleanup, listed: __log.filter((c) => c[0] === 'listFindings').length,
}}));
""")
    assert data["call"] == [
        "deleteDuplicates", 7, "f7", "/v/Game.iso",
        ["/v/Copy.iso", "/v/Disc.cue", "/v/Disc.bin"],
    ], data
    assert data["open"] is None
    # The Finding is read again: it shrinks or clears.
    assert data["listed"] == 1


def test_a_blocked_plan_deletes_nothing(tmp_path):
    data = _run(tmp_path, f"""
api.duplicateDeletePlan = __call('duplicateDeletePlan');
api.deleteDuplicates = __call('deleteDuplicates');
__answers.duplicateDeletePlan = {{ items: [{{ source_path: '/v/Copy.iso',
  delete_paths: ['/v/Copy.iso'], errors: ['Copy.iso changed'] }}], blocked: true }};
await findings.openCleanup({_ROW});
const result = await findings.confirmCleanup();
process.stdout.write(JSON.stringify({{
  result, called: __log.some((c) => c[0] === 'deleteDuplicates'),
}}));
""")
    assert data == {"result": None, "called": False}, data


def test_the_view_offers_the_delete_only_where_the_backend_does() -> None:
    view = _VIEW.read_text(encoding="utf-8")
    assert "row.kind === 'duplicate' && row.deletable" in view
    assert "findings.openCleanup(row)" in view
    assert "row.keeper_reason" in view and "row.not_deletable" in view
    # A keeper not yet proven says what to do first (#389, Codex
    # PRRT_kwDOQ9nULM64Lh-l); the delete itself is refused by the backend.
    assert "row.keeper_problem" in view
    assert "<DuplicateDeleteModal" in view
    modal = _MODAL.read_text(encoding="utf-8")
    # Confirm is off until there is a plan, and while it is blocked.
    assert "disabled={!plan || blocked || !!open?.error}" in modal
    endpoints = _ENDPOINTS.read_text(encoding="utf-8")
    assert "/delete-plan`" in endpoints
    assert "'X-CHD-Action-Confirm': CONFIRM.DELETE_FILE" in endpoints
