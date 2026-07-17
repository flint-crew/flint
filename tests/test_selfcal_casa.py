from __future__ import annotations

from pathlib import Path

import pytest
from casacore.tables import table

from flint.ms import MS
from flint.naming import get_selfcal_ms_name
from flint.selfcal.casa import (
    GainCalOptions,
    _process_gaincal_options,
    _skippable_ms_copy_and_clean,
)


def test_skippable_ms_copy_and_clean(ms_example) -> None:
    """Examine the conditions around self-calibration and the MS to see
    if the cleaning / copying steps can be skipped, e.g. in case of rerun"""
    ms = MS(path=Path(ms_example), column="DATA")
    out_ms_path = get_selfcal_ms_name(in_ms_path=ms.path)

    assert ms.path != out_ms_path
    assert not out_ms_path.exists()

    assert not _skippable_ms_copy_and_clean(
        ms=ms, out_ms_path=out_ms_path, rename_ms=False
    )

    # If rename is true then in all cases the base MS should not exist
    with pytest.raises(ValueError, match="No attempt to recover."):
        assert not _skippable_ms_copy_and_clean(
            ms=ms.with_options(path=Path("JACKSPARROW")),
            out_ms_path=out_ms_path,
            rename_ms=True,
        )

    ms.path.rename(out_ms_path)
    assert not ms.path.exists()
    assert out_ms_path.exists()
    with table(str(out_ms_path), ack=False) as tab:
        assert "DATA" in tab.colnames()

    assert _skippable_ms_copy_and_clean(ms=ms, out_ms_path=out_ms_path, rename_ms=True)


def test_process_gaincal_options() -> None:
    """Ensure the correct handling of the initialisation of GainCalOptions"""

    empty_gaincal = _process_gaincal_options()
    assert isinstance(empty_gaincal, GainCalOptions)

    gain_cal_options = GainCalOptions()
    new_options = _process_gaincal_options(
        gain_cal_options=gain_cal_options, update_gain_cal_options=None
    )
    assert new_options == gain_cal_options

    update_gain_cal_options = {"solint": "inf"}
    updated_options = _process_gaincal_options(
        gain_cal_options=gain_cal_options,
        update_gain_cal_options=update_gain_cal_options,
    )
    assert updated_options.solint == "inf"
