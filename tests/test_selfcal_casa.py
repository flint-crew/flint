from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from casacore.tables import table

from flint.ms import MS
from flint.naming import get_selfcal_ms_name
from flint.selfcal.casa import (
    GainCalOptions,
    _process_gaincal_options,
    _skippable_ms_copy_and_clean,
    check_for_valid_calibration_table,
)
from flint.utils import get_packaged_resource_path


def test_check_for_valid_calibration_table(tmpdir) -> None:
    """Basic checks around the calibration table"""

    tmp_path = Path(tmpdir)
    tmp_path.mkdir(parents=True, exist_ok=True)

    bad_file = tmp_path / "JACKSPARROWMATE.txt"
    bad_file.touch()
    assert not check_for_valid_calibration_table(caltable_path=bad_file)
    assert not check_for_valid_calibration_table(caltable_path=bad_file.parent)

    caltable_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB75061.EMU_1210+04A.beam08.round1.caltable.ch0000-0287.zip",
        )
    )
    assert caltable_zip.exists()
    outpath = tmp_path / "EXAMPLE"
    outpath.mkdir(exist_ok=True, parents=True)

    shutil.unpack_archive(caltable_zip, outpath)
    caltable_path = outpath / "SB75061.EMU_1210+04A.beam08.round1.caltable.ch0000-0287"
    assert caltable_path.exists()

    assert check_for_valid_calibration_table(caltable_path=caltable_path)


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
