"""Test utilities related to flagging measurement set operations"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from casacore.tables import table

from flint.flagging import flag_ms_zero_uvws, nan_zero_extreme_flag_ms
from flint.utils import get_packaged_resource_path


@pytest.fixture
def ms_example(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0.small.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "39400"

    shutil.unpack_archive(ms_zip, outpath)

    ms_path = Path(outpath) / "SB39400.RACS_0635-31.beam0.small.ms"

    return ms_path


def test_flag_ms_zero_uvws(ms_example):
    flag_ms_zero_uvws(ms=ms_example)

    with table(str(ms_example), ack=False) as tab:
        uvws = tab.getcol("UVW")
        flags = tab.getcol("FLAG")

        uvw_mask = np.all(uvws == 0, axis=1)

        assert np.all(flags[uvw_mask] == True)  # noQA: E712


def test_add_flag_ms_zero_uvws(ms_example):
    with table(str(ms_example), readonly=False, ack=False) as tab:
        uvws = tab.getcol("UVW")
        uvws[:1, :] = 0

        tab.putcol("UVW", uvws)

    flag_ms_zero_uvws(ms=ms_example)

    with table(str(ms_example), ack=False) as tab:
        uvws = tab.getcol("UVW")

        flags = tab.getcol("FLAG")

        uvw_mask = np.all(uvws == 0, axis=1)

        assert np.sum(uvw_mask) > 0
        assert np.all(flags[uvw_mask] == True)  # noQA: E712


def test_nan_zero_extreme_flag_ms(ms_example):
    """Makes sure that flagging the NaNs, UVWs of zero and
    extreme outliers works. Was added after introducing the
    chunking"""
    with table(str(ms_example), readonly=False, ack=False) as tab:
        uvws = tab.getcol("UVW")
        uvws[:10, :] = 0

        tab.putcol("UVW", uvws)

    nan_zero_extreme_flag_ms(ms=ms_example)

    with table(str(ms_example), ack=False) as tab:
        uvws = tab.getcol("UVW")

        flags = tab.getcol("FLAG")

        uvw_mask = np.all(uvws == 0, axis=1)

        assert np.sum(uvw_mask) > 0
        assert np.all(flags[uvw_mask] == True)  # noQA: E712


def test_nan_zero_extreme_flag_ms_with_chunks(ms_example):
    """Makes sure that flagging the NaNs, UVWs of zero and
    extreme outliers works. Was added after introducing the
    chunking.

    Same as above test but with chunk size"""
    with table(str(ms_example), readonly=False, ack=False) as tab:
        uvws = tab.getcol("UVW")
        uvws[:20, :] = 0

        tab.putcol("UVW", uvws)

    nan_zero_extreme_flag_ms(ms=ms_example, chunk_size=1)

    with table(str(ms_example), ack=False) as tab:
        uvws = tab.getcol("UVW")

        flags = tab.getcol("FLAG")

        uvw_mask = np.all(uvws == 0, axis=1)

        assert np.sum(uvw_mask) > 0
        assert np.all(flags[uvw_mask] == True)  # noQA: E712


def test_nan_zero_extreme_flag_ms_with_chunks_and_datanan(ms_example):
    """Makes sure that flagging the NaNs, UVWs of zero and
    extreme outliers works. Was added after introducing the
    chunking.

    Same as above test but with chunk size and naning data when flags are true"""
    with table(str(ms_example), readonly=False, ack=False) as tab:
        uvws = tab.getcol("UVW")
        uvws[:20, :] = 0
        original_data = tab.getcol("DATA")
        tab.putcol("UVW", uvws)

    nan_zero_extreme_flag_ms(ms=ms_example, chunk_size=1, nan_data_on_flag=True)

    with table(str(ms_example), ack=False) as tab:
        uvws = tab.getcol("UVW")

        flags = tab.getcol("FLAG")
        data = tab.getcol("DATA")
        uvw_mask = np.all(uvws == 0, axis=1)

        assert np.sum(np.isnan(data)) == np.sum(flags)
        assert np.sum(np.isnan(data)) > np.sum(np.isnan(original_data))
        assert np.sum(uvw_mask) > 0
        assert np.all(flags[uvw_mask] == True)  # noQA: E712
