from __future__ import annotations

import ast
from pathlib import Path

import pytest

import flint.prefect
from flint.logging import logger

from .conftest import which


def test_no_unmapped_outside_map():
    """``unmapped`` is a ``tuple`` subclass that only ``Task.map`` unwraps. Passing it
    to ``Task.submit`` silently hands the annotation itself to the task function."""

    def is_unmapped(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "unmapped"
        )

    offenders = []
    for path in Path(flint.prefect.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "map"
            ):
                continue
            args: list[ast.expr] = [*node.args, *[kw.value for kw in node.keywords]]
            if any(is_unmapped(arg) for arg in args):
                offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node.func)}")

    assert not offenders, f"unmapped() used outside of .map(): {offenders}"


@pytest.mark.require_singularity
def test_singularity():
    which_singularity = which("singularity")
    logger.info(f"Singularity is installed at: {which_singularity}")
    assert which_singularity is not None
