"""Tests around utility helper functions for self-calibration"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flint.options import MS
from flint.selfcal.utils import (
    consider_skip_selfcal_on_round,
    create_and_check_caltable_path,
    get_channel_ranges_given_nspws,
    get_channel_ranges_given_nspws_for_ms,
)
from flint.utils import get_packaged_resource_path


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
