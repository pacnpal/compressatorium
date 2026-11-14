"""User-facing backend text never uses "verified" as a state (#298).

CONTEXT.md: a check proves Intact, Round-tripped or Recognized, and "verify"
is only the action. Progress, status and error messages that services and
routes hand to the UI are string literals in ``app/``; this walks them with
``ast`` and flags any sentence (a literal with a space in it) that says
"verified". Bare keys and route paths such as ``"verified"`` or
``"/verified"`` are API names, not prose, and log lines and docstrings are not
shown to users, so neither is checked.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
_STATE = re.compile(r"\bverified\b", re.IGNORECASE)
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """ids of string nodes that are docstrings or log-call arguments."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant,
            ):
                skip.add(id(body[0].value))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
        ):
            for arg in node.args:
                for sub in ast.walk(arg):
                    skip.add(id(sub))
    return skip


def test_user_facing_backend_strings_do_not_say_verified():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _skipped_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and " " in node.value.strip()
                and _STATE.search(node.value)
            ):
                rel = path.relative_to(APP.parent)
                offenders.append(f"{rel}:{node.lineno}: {node.value.strip()!r}")
    assert offenders == [], "\n".join(offenders)
