from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flint.exceptions import MSError
from flint.ms import MS
from flint.selfcal.casa import (
    GainCalOptions,
    _process_gaincal_options,
    check_for_valid_calibration_table,
    gaincal_applycal_ms,
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


def test_gaincal_applycal_ms_needs_data_column(tmpdir) -> None:
    """casa only calibrates DATA, so other columns are refused"""
    casa_container = Path(tmpdir) / "casa.sif"
    casa_container.touch()

    with pytest.raises(MSError, match="casa calibrates DATA"):
        gaincal_applycal_ms(
            ms=MS(path=Path("JACKSPARROW.ms"), column="CORRECTED_DATA"),
            casa_container=casa_container,
        )


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
