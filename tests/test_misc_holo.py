"""Tests around the holography hlper utility"""

from __future__ import annotations

from flint.misc.holo import get_parser


def test_get_parser() -> None:
    """A dumb tester for the argument parser"""
    _ = get_parser()
