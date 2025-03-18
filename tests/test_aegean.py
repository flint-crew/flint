"""Tests around BANE and aegean, which are one of the source finding
tools used in flint. BANE is also used to derive signal maps that ultimately
feed the clean mask creation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.exceptions import AttemptRerunException
from flint.source_finding.aegean import (
    AegeanOptions,
    BANEOptions,
    _bane_output_callback,
    _get_aegean_command,
    _get_bane_command,
    get_parser,
)


def test_get_parser():
    """Make sure that there are no conflicting options when creating the parser object"""
    _ = get_parser()


def test_bane_deadlock_callback():
    """Noticed that BANE sometimes goes into a 'deadlock' state, and will
    seemingly always issue an log (from scipy, presumably, arrr)
    around interpolation not being strictly ascending"""

    lines = (
        "30146:INFO using 8 cores",
        "30146:INFO using 7 stripes",
        "30343:WARNING The points in dimension 0 must be strictly ascending or descending",
    )
    _bane_output_callback(line=lines[0])
    _bane_output_callback(line=lines[1])

    with pytest.raises(AttemptRerunException):
        _bane_output_callback(line=lines[2])


def test_bane_options():
    bane_opts = BANEOptions(box_size=(2, 1), grid_size=(20, 10))

    assert isinstance(bane_opts, BANEOptions)

    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )

    assert isinstance(bane_str, str)

    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1} --grid 20 10 --box 2 1"
    assert expected == bane_str


def test_bane_options_with_defaults():
    bane_opts = BANEOptions(box_size=None, grid_size=None)

    assert isinstance(bane_opts, BANEOptions)

    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )

    assert isinstance(bane_str, str)

    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1}"
    assert expected == bane_str

    bane_opts = BANEOptions(box_size=(3, 5), grid_size=None)
    bane_str = _get_bane_command(
        image=Path("this/is/a/test.fits"), bane_options=bane_opts
    )
    expected = f"BANE this/is/a/test.fits --cores {bane_opts.cores} --stripes {bane_opts.cores - 1} --box 3 5"
    assert expected == bane_str


def test_aegean_options():
    """Testing basic aegean options creation and expected command"""
    aegean_options = AegeanOptions(nocov=True, maxsummits=4, autoload=True)

    # This are silly tests
    assert isinstance(aegean_options, AegeanOptions)
    assert aegean_options.maxsummits == 4

    ex_path = Path("this/is/a/test.fits")
    aegean_command = _get_aegean_command(
        image=ex_path, base_output="example", aegean_options=aegean_options
    )

    expected_cmd = "aegean this/is/a/test.fits --autoload --nocov --maxsummits 4 --table example.fits"
    assert aegean_command == expected_cmd

    aegean_options2 = AegeanOptions(nocov=False, maxsummits=40, autoload=False)
    aegean_command2 = _get_aegean_command(
        image=ex_path, base_output="example", aegean_options=aegean_options2
    )

    expected_cmd2 = "aegean this/is/a/test.fits --maxsummits 40 --table example.fits"
    assert aegean_command2 == expected_cmd2
