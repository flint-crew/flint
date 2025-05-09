"""Test utilities related to flagging measurement set operations"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from casacore.tables import table

from flint.containers import get_known_container_path
from flint.flagging import (
    flag_ms_aoflagger,
    flag_ms_zero_uvws,
    nan_zero_extreme_flag_ms,
)
from flint.ms import MS


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


# Vontainer related options
# if which("singularity") is None:
#     pytest.skip("Singularity is not installed", allow_module_level=True)


@pytest.mark.slow
@pytest.mark.require_singularity
def test_aoflagger(flint_containers, ms_example) -> None:
    """Ensure we can run a aoflagger recipe"""

    aoflagger_path = get_known_container_path(
        container_directory=flint_containers, name="aoflagger"
    )
    assert isinstance(aoflagger_path, Path)
    assert aoflagger_path.exists()

    ms = MS(path=ms_example, column="DATA")

    with table(str(ms.path), readonly=False) as tab:
        orig_flags = tab.getcol("FLAG")
        tab.putcol("FLAG", np.zeros_like(orig_flags))
        # Reset the flags to ensure aoflagger does the
        # right set of things
        pre_flag = np.sum(orig_flags)

    flag_ms = flag_ms_aoflagger(ms=ms, container=aoflagger_path)

    assert isinstance(flag_ms, MS)
    assert flag_ms.path == ms.path

    with table(str(ms.path), readonly=True) as tab:
        # uvws shape is (row, coord)
        uvws = tab.getcol("UVW")
        # flags shapoe is {row, channel, pol}
        flags = tab.getcol("FLAG")
        post_flag = np.sum(flags)
        u = uvws[:, 0]
        assert np.all(u[~flags[:, 0, 0]] != 0)

    assert pre_flag == post_flag
