from __future__ import annotations

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
