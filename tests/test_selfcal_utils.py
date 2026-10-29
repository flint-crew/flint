"""Tests around utility helper functions for self-calibration"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from casacore.tables import table

from flint.ms import preprocess_askap_ms
from flint.naming import get_selfcal_ms_name
from flint.options import MS
from flint.selfcal.utils import (
    consider_skip_selfcal_on_round,
    create_and_check_caltable_path,
    get_channel_ranges_given_nspws,
    get_channel_ranges_given_nspws_for_ms,
    prepare_ms_for_selfcal,
)
from flint.utils import get_packaged_resource_path

from .conftest import add_corrected_data, get_columns


def test_create_solution_path():
    """Create the path to a solutions file, including appropriate handling of a channel range"""

    ms_path = Path("/jack/has/a/measurement_set.ms")
    cal_table = create_and_check_caltable_path(ms=MS.cast(ms_path))

    assert isinstance(cal_table, Path)
    assert cal_table == Path("/jack/has/a/measurement_set.caltable")

    cal_table = create_and_check_caltable_path(
        ms=MS.cast(ms_path), channel_range=(0, 143)
    )

    assert isinstance(cal_table, Path)
    assert cal_table == Path("/jack/has/a/measurement_set.caltable.ch0000-0143")

    cal_table = create_and_check_caltable_path(
        ms=MS.cast(Path("jack/measurement_set.ms"))
    )
    assert cal_table == Path.cwd() / "jack/measurement_set.caltable"


@pytest.fixture
def casda_example(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="scienceData.EMU_0529-60.SB50538.EMU_0529-60.beam08_averaged_cal.leakage.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "extract"

    shutil.unpack_archive(ms_zip, outpath)

    ms_path = (
        Path(outpath)
        / "scienceData.EMU_0529-60.SB50538.EMU_0529-60.beam08_averaged_cal.leakage.ms"
    )

    return ms_path


def test_get_channel_ranges_given_nspws_for_ms(casda_example):
    """Obtain channel ranges given an MS"""
    ms_path = Path(casda_example)
    chan_ranges = get_channel_ranges_given_nspws_for_ms(ms=ms_path, nspw=1)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 1
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 287

    chan_ranges = get_channel_ranges_given_nspws_for_ms(ms=ms_path, nspw=4)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 4
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 71

    assert chan_ranges[1][0] == 72
    assert chan_ranges[1][1] == 143
    assert chan_ranges[-1][1] == 287


def test_get_channel_ranges_given_nspws_for_ms_with_oddstep(casda_example):
    """Obtain channel ranges given an MS"""
    ms_path = Path(casda_example)
    chan_ranges = get_channel_ranges_given_nspws_for_ms(ms=ms_path, nspw=5)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 5
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 57

    assert chan_ranges[1][0] == 58
    assert chan_ranges[1][1] == 115

    assert chan_ranges[-1][1] == 287


def test_get_channel_ranges_given_nspw():
    """At times channel ranges given a number of channels and number of windows needs to be created"""
    chan_ranges = get_channel_ranges_given_nspws(num_channels=288, nspws=1)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 1
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 287

    chan_ranges = get_channel_ranges_given_nspws(num_channels=288, nspws=4)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 4
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 71

    assert chan_ranges[1][0] == 72
    assert chan_ranges[1][1] == 143
    assert chan_ranges[-1][1] == 287


def test_get_channel_ranges_given_nspw_odd_steps():
    """At times channel ranges given a number of channels and number of windows needs to be created.
    This has odd channel steps"""
    chan_ranges = get_channel_ranges_given_nspws(num_channels=288, nspws=5)
    assert isinstance(chan_ranges, tuple)
    assert len(chan_ranges) == 5
    assert chan_ranges[0][0] == 0
    assert chan_ranges[0][1] == 57

    assert chan_ranges[1][0] == 58
    assert chan_ranges[1][1] == 115

    assert chan_ranges[-1][1] == 287


def test_consider_skip_selfcal():
    """Ensure that the skipping behaves as expected"""

    # None here means nothing should be skipped
    res = consider_skip_selfcal_on_round(current_round=1, skip_selfcal_on_rounds=None)

    assert not res

    res = consider_skip_selfcal_on_round(current_round=1, skip_selfcal_on_rounds=[2, 3])
    assert not res

    res = consider_skip_selfcal_on_round(current_round=2, skip_selfcal_on_rounds=[2, 3])
    assert res
    res = consider_skip_selfcal_on_round(current_round=2, skip_selfcal_on_rounds=2)
    assert res


@pytest.mark.parametrize("keep_old_columns", [False, True])
def test_prepare_ms_for_selfcal_resumes(ms_example, keep_old_columns) -> None:
    """Preparing can be run again after stopping part way through moving columns, or after finishing"""
    ms = MS(path=Path(ms_example), column="CORRECTED_DATA")
    add_corrected_data(ms_path=ms.path)
    with table(str(ms.path), readonly=False, ack=False) as tab:
        if keep_old_columns:
            tab.renamecol(oldname="DATA", newname="DATA_ROUND0")
        else:
            tab.removecols(columnnames="DATA")

    prepared_ms = prepare_ms_for_selfcal(
        ms=ms, round=1, keep_old_columns=keep_old_columns
    )
    assert prepared_ms.path == get_selfcal_ms_name(in_ms_path=ms.path, round=1)
    colnames = get_columns(ms_path=prepared_ms.path)
    assert "DATA" in colnames
    assert "CORRECTED_DATA" not in colnames
    assert ("DATA_ROUND0" in colnames) == keep_old_columns

    # Like the CORRECTED_DATA a potato peel leaves beside DATA
    add_corrected_data(ms_path=prepared_ms.path)
    assert (
        prepare_ms_for_selfcal(ms=ms, round=1, keep_old_columns=keep_old_columns)
        == prepared_ms
    )

    with pytest.raises(FileNotFoundError):
        prepare_ms_for_selfcal(ms=ms.with_options(path=Path("JACKSPARROW.ms")), round=1)


def test_prepare_ms_for_selfcal_keep_old_columns_round_zero(ms_example) -> None:
    """Round 0 can only keep old columns when DATA is nominated, as nothing is replaced"""
    add_corrected_data(ms_path=ms_example)
    ms = MS(path=Path(ms_example), column="CORRECTED_DATA")

    with pytest.raises(ValueError, match="no earlier round"):
        prepare_ms_for_selfcal(ms=ms, round=0, keep_old_columns=True)
    assert ms.path.exists()

    prepared_ms = prepare_ms_for_selfcal(
        ms=ms.with_options(column="DATA"), round=0, keep_old_columns=True
    )
    assert prepared_ms.path.name.endswith(".round0.ms")


def test_prepare_ms_for_selfcal(ms_example) -> None:
    """The imaged column becomes DATA in a renamed MS, and INSTRUMENT_DATA is kept"""
    ms = preprocess_askap_ms(ms=ms_example)
    assert "INSTRUMENT_DATA" in get_columns(ms_path=ms.path)

    for round in (1, 2):
        corrected_data = add_corrected_data(ms_path=ms.path)
        ms = prepare_ms_for_selfcal(
            ms=ms.with_options(column="CORRECTED_DATA"), round=round
        )

        assert ms.path.name.endswith(f".round{round}.ms")
        assert ms.column == "DATA"
        colnames = get_columns(ms_path=ms.path)
        assert "CORRECTED_DATA" not in colnames
        assert "INSTRUMENT_DATA" in colnames
        assert not any(col.startswith("DATA_ROUND") for col in colnames)
        with table(str(ms.path), ack=False) as tab:
            assert np.allclose(tab.getcol("DATA"), corrected_data, equal_nan=True)


def test_prepare_ms_for_selfcal_keep_old_columns(ms_example) -> None:
    """Each round's replaced DATA is kept under the name of its round"""
    ms = preprocess_askap_ms(ms=ms_example)
    with table(str(ms.path), ack=False) as tab:
        original_data = tab.getcol("DATA")

    corrected_data = add_corrected_data(ms_path=ms.path)
    ms = prepare_ms_for_selfcal(
        ms=ms.with_options(column="CORRECTED_DATA"), round=1, keep_old_columns=True
    )
    colnames = get_columns(ms_path=ms.path)
    assert {"DATA", "DATA_ROUND0", "INSTRUMENT_DATA"} <= set(colnames)
    assert "CORRECTED_DATA" not in colnames
    with table(str(ms.path), ack=False) as tab:
        assert np.allclose(tab.getcol("DATA_ROUND0"), original_data, equal_nan=True)
        assert np.allclose(tab.getcol("DATA"), corrected_data, equal_nan=True)

    add_corrected_data(ms_path=ms.path)
    ms = prepare_ms_for_selfcal(
        ms=ms.with_options(column="CORRECTED_DATA"), round=2, keep_old_columns=True
    )
    assert {"DATA_ROUND0", "DATA_ROUND1"} <= set(get_columns(ms_path=ms.path))
