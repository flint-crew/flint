"""Tests around the crystalball prediction"""

from __future__ import annotations

from flint.predict.crystalball import get_parser


def test_get_parser():
    """A simple test to obtain the parser. Tests imports as well."""
    _ = get_parser()
