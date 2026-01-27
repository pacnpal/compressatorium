import ast
from pathlib import Path

import pytest


def _has_future_annotations(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(name.name == "annotations" for name in node.names):
                return True
    return False


def _is_list_subscript(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "list"
    )


def test_chdman_annotations_py38_compatible():
    """
    Fail if chdman.py uses list[...] annotations without future annotations.

    This pattern breaks on Python 3.8 where built-in generics aren't supported.
    """
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "app" / "services" / "chdman.py"
    tree = ast.parse(src_path.read_text())

    if _has_future_annotations(tree):
        return

    # Only scan annotation contexts.
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and _is_list_subscript(node.annotation):
            pytest.fail(
                "Found list[...] annotation in chdman.py without "
                "from __future__ import annotations; breaks Python 3.8."
            )

