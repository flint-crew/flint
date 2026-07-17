"""Tests around the casa self-calibration tooling"""

from __future__ import annotations

from pathlib import Path

from flint.casa import args_to_casa_task_string
from flint.selfcal.casa import GainCalOptions, _process_gaincal_options


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


def test_args_to_casa_task_str():
    """Ensure we can transform casa style function calls to strings"""

    transform = args_to_casa_task_string(
        task="mstransform",
        regridms=True,
        nspw=1,
        mode="channel",
        nchan=-1,
    )
    assert isinstance(transform, str)

    expected = "casa -c mstransform(regridms=True,nspw=1,mode='channel',nchan=-1)"
    assert transform == expected


def test_args_to_casa_task_str_arg_list():
    """Same as above but should an argument be a list of paths (for example) needs help"""

    paths = [
        Path("/jack/dataset1.ms"),
        Path("/jack/dataset2.ms"),
    ]
    applycal = args_to_casa_task_string(
        task="applycal",
        vis=Path("/some/other/ship/visibility.ms"),
        gaintable=paths,
    )

    expected = "casa -c applycal(vis='/some/other/ship/visibility.ms',gaintable=('/jack/dataset1.ms','/jack/dataset2.ms'))"
    assert expected == applycal
