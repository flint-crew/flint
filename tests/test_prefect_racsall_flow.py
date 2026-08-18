"""Tests around the prefect racs-all flow"""

from __future__ import annotations

from argparse import ArgumentParser

from flint.prefect.flows.racs_all_continuum_selfcal import get_parser


def test_get_parser() -> None:
    """Simple test to ensure that the parser can be loaded. Useful
    to ensure classes/imports are not broker"""
    parser = get_parser()

    assert isinstance(parser, ArgumentParser)


def test_get_parser_has_no_run_polarisation() -> None:
    """Polarisation orchestration moved to the racs-all flow-of-flows

    This flow only does continuum imaging/self-cal and should no longer expose
    run_polarisation or any other PolFieldOptions flag.
    
    """
    parser = get_parser()
    option_strings = {
        option_string
        for action in parser._actions
        for option_string in action.option_strings
    }
    assert "--run-polarisation" not in option_strings
