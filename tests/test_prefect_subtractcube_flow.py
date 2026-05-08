"""Tests around the subtract cube imaging flow"""

from __future__ import annotations

from flint.prefect.flows.subtract_cube_pipeline import get_parser


def test_get_parser():
    """Make sure the parser can actually be built"""
    # This is a silly one but I was bitten by it not working, arrrrhh matey
    _ = get_parser()
