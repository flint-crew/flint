"""Some tests related to using aoccalibrate related things"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from flint.bptools.smoother import (
    divide_bandpass_by_ref_ant,
    divide_bandpass_by_ref_ant_preserve_phase,
    smooth_bandpass_complex_gains,
    smooth_data,
)
from flint.calibrate.aocalibrate import (
    AOSolutions,
    CalibrateOptions,
    FlaggedAOSolution,
    calibrate_options_to_command,
    flag_aosolutions,
    plot_solutions,
    select_refant,
)
from flint.utils import get_packaged_resource_path


def test_calibrate_options_to_command():
    default_cal = CalibrateOptions(datacolumn="DATA", m=Path("/example/1934.model"))
    ex_ms_path = Path("/example/data.ms")
    ex_solutions_path = Path("/example/sols.calibrate")

    cmd = calibrate_options_to_command(
        calibrate_options=default_cal,
        ms_path=ex_ms_path,
        solutions_path=ex_solutions_path,
    )

    assert (
        cmd
        == "calibrate -datacolumn DATA -m /example/1934.model -i 100 /example/data.ms /example/sols.calibrate"
    )


def test_calibrate_options_to_command2():
    default_cal = CalibrateOptions(
        datacolumn="DATA",
        m=Path("/example/1934.model"),
        i=40,
        p=(Path("amps.plot"), Path("phase.plot")),
    )
    ex_ms_path = Path("/example/data.ms")
    ex_solutions_path = Path("/example/sols.calibrate")

    cmd = calibrate_options_to_command(
        calibrate_options=default_cal,
        ms_path=ex_ms_path,
        solutions_path=ex_solutions_path,
    )

    assert (
        cmd
        == "calibrate -datacolumn DATA -m /example/1934.model -i 40 -p amps.plot phase.plot /example/data.ms /example/sols.calibrate"
    )


def test_calibrate_options_to_command3():
    default_cal = CalibrateOptions(
        datacolumn="DATA",
        m=Path("/example/1934.model"),
        i=40,
        p=(Path("amps.plot"), Path("phase.plot")),
        maxuv=5000,
        minuv=300,
    )
    ex_ms_path = Path("/example/data.ms")
    ex_solutions_path = Path("/example/sols.calibrate")

    cmd = calibrate_options_to_command(
        calibrate_options=default_cal,
        ms_path=ex_ms_path,
        solutions_path=ex_solutions_path,
    )

    assert (
        cmd
        == "calibrate -datacolumn DATA -m /example/1934.model -minuv 300.0 -maxuv 5000.0 -i 40 -p amps.plot phase.plot /example/data.ms /example/sols.calibrate"
    )


@pytest.fixture
def ao_sols(tmpdir):
    ao_sols = Path(
        get_packaged_resource_path(
            package="flint.data.tests", filename="SB39433.B1934-638.beam0.calibrate.bin"
        )
    )

    out_ao_sols = Path(tmpdir) / ao_sols.name

    shutil.copyfile(ao_sols, out_ao_sols)

    return out_ao_sols


@pytest.fixture
def ao_sols_known_bad(tmpdir):
    # The file contains a binary solutions file that failed previously.
    # It was fixed by testing for all nans in the `flint.bptools.smoother.smooth_data`
    # function.
    ao_sols = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB38969.B1934-638.beam35.aocalibrate.bin",
        )
    )

    out_ao_sols = Path(tmpdir) / ao_sols.name

    shutil.copyfile(ao_sols, out_ao_sols)

    return out_ao_sols


def test_known_bad_sols(ao_sols_known_bad):
    flag_aosolutions(solutions_path=ao_sols_known_bad, plot_solutions_throughout=False)


def test_flagged_aosols_mesh(ao_sols_known_bad):
    flagged_sols = flag_aosolutions(
        solutions_path=ao_sols_known_bad,
        mesh_ant_flags=True,
        plot_solutions_throughout=False,
        smooth_solutions=False,
    )
    assert isinstance(flagged_sols, FlaggedAOSolution)
    assert len(flagged_sols.plots) == 0


def test_flagged_aosols(ao_sols_known_bad):
    """Ensure smoothing and flagging operations work. When smoothing
    more plots should be created."""
    flagged_sols = flag_aosolutions(
        solutions_path=ao_sols_known_bad,
        plot_solutions_throughout=True,
        smooth_solutions=True,
    )
    assert isinstance(flagged_sols, FlaggedAOSolution)
    assert len(flagged_sols.plots) == 9
    assert isinstance(flagged_sols.path, Path)

    flagged_sols = flag_aosolutions(
        solutions_path=ao_sols_known_bad,
        plot_solutions_throughout=True,
        smooth_solutions=False,
    )
    assert isinstance(flagged_sols, FlaggedAOSolution)
    assert len(flagged_sols.plots) == 6
    assert isinstance(flagged_sols.path, Path)


def test_load_aosols(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    assert ao.nant == 36
    assert ao.nchan == 288
    assert ao.npol == 4


def test_aosols_bandpass_plot(ao_sols):
    # This is just a dumb test to make sure the function runs
    plot_solutions(solutions=ao_sols, ref_ant=0)
    plot_solutions(solutions=ao_sols, ref_ant=None)


def test_aosols_all_nans_smooth_data(ao_sols):
    ao = AOSolutions.load(ao_sols)
    ao.bandpass[0, 20, :, :] = np.nan
    assert np.all(~np.isfinite(ao.bandpass[0, 20, :, 0]))

    smoothed = smooth_data(
        data=ao.bandpass[0, 20, :, 0].real, window_size=16, polynomial_order=4
    )
    assert np.all(~np.isfinite(smoothed))


def test_smooth_bandpass_complex_gains_nans(ao_sols):
    ao = AOSolutions.load(ao_sols)
    ao.bandpass[0, 20, :, :] = np.nan
    assert np.all(~np.isfinite(ao.bandpass[0, 20, :, 0]))

    smoothed = smooth_bandpass_complex_gains(
        complex_gains=ao.bandpass[0], window_size=16, polynomial_order=4
    )
    assert np.all(~np.isfinite(smoothed[20, :, 0]))


def test_smooth_bandpass_complex_gains_nans_with_refant(ao_sols):
    ao = AOSolutions.load(ao_sols)
    ao.bandpass[0, 20, :, :] = np.nan
    assert np.all(~np.isfinite(ao.bandpass[0, 20, :, 0]))

    ref = divide_bandpass_by_ref_ant(complex_gains=ao.bandpass[0], ref_ant=0)

    smoothed = smooth_bandpass_complex_gains(
        complex_gains=ref, window_size=16, polynomial_order=4
    )
    assert np.all(~np.isfinite(smoothed[20, :, 0]))


def test_aosols_bandpass_ref_nu_rank_error(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    # This should raise an assertion error since the data shape is not right
    with pytest.raises(AssertionError) as _:
        divide_bandpass_by_ref_ant(complex_gains=ao.bandpass, ref_ant=0)


def test_aosols_bandpass_ref_nu(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    complex_gains = divide_bandpass_by_ref_ant(complex_gains=ao.bandpass[0], ref_ant=0)

    expected = np.array(
        [
            0.11008759 - 0.00000000e00j,
            0.11009675 - 4.33444224e-19j,
            0.11017988 - 0.00000000e00j,
            0.10990718 - 0.00000000e00j,
            0.11060902 - 8.66905258e-19j,
        ]
    )
    assert np.allclose(expected, complex_gains[0, :5, 0])


def test_aosols_bandpass_ref_nu_preserve_phase(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    complex_gains = divide_bandpass_by_ref_ant_preserve_phase(
        complex_gains=ao.bandpass[0], ref_ant=0
    )

    x_angle = np.angle(complex_gains[0, :, 0])
    y_angle = np.angle(complex_gains[0, :, 3])

    assert np.allclose(x_angle[np.isfinite(x_angle)], 0)
    assert np.allclose(y_angle[np.isfinite(y_angle)], 0)

    expected = np.array(
        [
            -0.10846614 - 0.01465966j,
            -0.10776107 - 0.01495074j,
            -0.10728749 - 0.01611982j,
            -0.10742277 - 0.01654671j,
        ]
    )

    print(expected)
    print(complex_gains[1, :4, 0])

    assert np.allclose(expected, complex_gains[1, :4, 0])


def test_ref_ant_selection(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    ref_ant = select_refant(bandpass=ao.bandpass)

    assert ref_ant == 0


def test_ref_ant_selection_with_assert(ao_sols):
    ao = AOSolutions.load(path=ao_sols)

    # This ref ant selection function expects a rank of 4
    with pytest.raises(AssertionError) as _:
        select_refant(bandpass=ao.bandpass[0])


# TODO: Need to write more tests for the smoothing and other things
