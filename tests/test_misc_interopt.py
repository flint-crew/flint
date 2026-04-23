"""Tests around dealing with bespoke compatibility issues and quirks specific
to casda data. Mostly differences or changes."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from casacore.tables import table

from flint.misc.interopt import (
    flag_antenna_from_casda_bandpass_table,
    get_flagged_antenna_casda_solutions,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def casda_bandpass_table(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="calparameters.1934_bp.SB83782.tab.zip",
        )
    )
    outpath = Path(tmpdir) / "bandpass_table"

    shutil.unpack_archive(ms_zip, outpath)

    bandpass_table_path = Path(outpath) / "calparameters.1934_bp.SB83782.tab"

    return bandpass_table_path


def test_get_flagged_antenna_casda_solutions(casda_bandpass_table) -> None:
    """Load a casda table (the example is from a reference field) and see if the expected
    number of bad antennas is recovered"""
    result = get_flagged_antenna_casda_solutions(bandpass_table=casda_bandpass_table)

    assert len(result) == 36
    assert np.all(result[22] == np.array([2, 7, 26, 29]))
    assert np.all(result[34] == np.array([2, 7, 16, 26, 29]))


def test_get_flaffed_antenna_casda_solutions_raise_error(casda_bandpass_table) -> None:
    """Basic check around the column names being properly examined"""
    # It works
    result = get_flagged_antenna_casda_solutions(bandpass_table=casda_bandpass_table)

    assert len(result) == 36

    # Now rename
    with table(str(casda_bandpass_table), readonly=False, ack=False) as tab:
        tab.renamecol("BANDPASS", "JACKSPARROW")
        tab.flush()

    with pytest.raises(ValueError):
        get_flagged_antenna_casda_solutions(bandpass_table=casda_bandpass_table)


def test_flag_antenna_from_casda_bandpass_table(
    casda_bandpass_table, ms_example
) -> None:
    """Check on the flagging infrastructure between bandpass table and a slim measurement set"""

    _ = flag_antenna_from_casda_bandpass_table(
        ms=ms_example, bandpass_table=casda_bandpass_table
    )
