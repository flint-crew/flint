"""Tests around the prefect racs-all flow"""

from __future__ import annotations

from argparse import ArgumentParser

from flint.prefect.flows.racs_all_continuum_selfcal import get_parser


def test_get_parser() -> None:
    """Simple test to ensure that the parser can be loaded. Useful
    to ensure classes/imports are not broker"""
    parser = get_parser()

    assert isinstance(parser, ArgumentParser)
