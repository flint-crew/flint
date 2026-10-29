from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from flint.exceptions import MSError
from flint.ms import MS
from flint.selfcal.casa import (
    GainCalOptions,
    _process_gaincal_options,
    check_for_valid_calibration_table,
    cli,
    gaincal_applycal_ms,
)
from flint.utils import get_packaged_resource_path

from .conftest import add_corrected_data


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


def test_cli_checks_casa_container_first(ms_example, tmp_path, monkeypatch) -> None:
    """A missing casa container is caught before the MS is renamed"""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flint_gaincal",
            "gaincal",
            str(ms_example),
            "--casa-container",
            str(tmp_path / "casa.sif"),
        ],
    )
    with pytest.raises(FileNotFoundError, match="casa_container"):
        cli()
    assert ms_example.exists()


def test_cli_refuses_to_overwrite_corrected_data(
    ms_example, tmp_path, monkeypatch
) -> None:
    """Calibrating DATA would lose an existing CORRECTED_DATA"""
    casa_container = tmp_path / "casa.sif"
    casa_container.touch()
    add_corrected_data(ms_path=ms_example)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flint_gaincal",
            "gaincal",
            str(ms_example),
            "--casa-container",
            str(casa_container),
        ],
    )
    with pytest.raises(MSError, match="CORRECTED_DATA"):
        cli()
    assert ms_example.exists()


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
