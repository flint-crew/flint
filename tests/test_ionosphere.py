"""Tests for the spinifex ionospheric Faraday rotation correction"""

from __future__ import annotations

import shutil
from pathlib import Path

import astropy.units as u
import h5py
import numpy as np
import pytest
from astropy.time import Time
from capn_crunch import create_options_from_parser
from casacore.tables import table

from flint.ionosphere import IonosphereOptions, correct_ionosphere, get_parser
from flint.ms import HistoryEntry, has_history_entry
from flint.options import MS, PolFieldOptions
from flint.utils import get_packaged_resource_path

# The day of the IONEX map bundled with spinifex
IONEX_DAY = Time("2024-12-14T10:00:00")


@pytest.fixture
def polarised_ms(ms_example: Path) -> MS:
    """The example MS moved to the day of the bundled IONEX map, holding a Stokes Q source."""
    with table(str(ms_example), readonly=False, ack=False) as tab:
        shift = IONEX_DAY.mjd * 86400 - tab.getcol("TIME").min()
        for column in ("TIME", "TIME_CENTROID"):
            tab.putcol(column, tab.getcol(column) + shift)

        # Stokes I = Q = 1
        data = np.zeros(tab.getcol("DATA").shape, dtype=np.complex64)
        data[..., 0] = 2
        tab.putcol("DATA", data)
        tab.putcol("FLAG", np.zeros(data.shape, dtype=bool))

    return MS(path=ms_example, column="DATA")


@pytest.fixture
def ionex_directory(tmp_path: Path) -> Path:
    """The bundled IONEX map, also standing in for the next day's."""
    from spinifex.ionospheric.ionex_download import new_cddis_format

    ionex_map = get_packaged_resource_path(
        package="spinifex.data.tests",
        filename="IGS0OPSFIN_20243490000_01D_02H_GIM.INX.gz",
    )
    directory = tmp_path / "ionex"
    directory.mkdir()
    for day in (IONEX_DAY, IONEX_DAY + 1 * u.day):
        shutil.copy(
            ionex_map, directory / Path(new_cddis_format(day, prefix="igs")).name
        )

    return directory


@pytest.fixture
def options(ionex_directory: Path) -> IonosphereOptions:
    return IonosphereOptions(
        ionex_directory=ionex_directory, server="cddis", prefix="igs"
    )


def read_column(ms: MS, column: str) -> np.ndarray:
    with table(str(ms.path), ack=False) as tab:
        return tab.getcol(column)


def test_correct_ionosphere(polarised_ms: MS, options: IonosphereOptions):
    corrected_ms = correct_ionosphere(polarised_ms, options)

    assert corrected_ms.column == "IONO_CORRECTED_DATA"
    assert has_history_entry(corrected_ms, HistoryEntry.IONOSPHERE_CORRECTED)
    with h5py.File(corrected_ms.h5parm) as h5parm:
        rm = h5parm["sol000/rotationmeasure000/val"][:].mean()

    corrected = read_column(corrected_ms, "IONO_CORRECTED_DATA")
    stokes_i = (corrected[..., 0] + corrected[..., 3]).real
    stokes_q = (corrected[..., 0] - corrected[..., 3]).real
    stokes_u = (corrected[..., 1] + corrected[..., 2]).real
    assert np.allclose(stokes_i, 2, atol=1e-5)

    # The source started with no Stokes U, so the angle it has gained is the correction
    with table(f"{polarised_ms.path}/SPECTRAL_WINDOW", ack=False) as spw:
        wavelength_sq = (299792458.0 / spw.getcol("CHAN_FREQ")[0]) ** 2
    angle = 0.5 * np.arctan2(stokes_u, stokes_q)
    assert np.allclose(angle[0] / wavelength_sq, -rm, rtol=1e-2)


def test_correct_ionosphere_twice_does_not_rotate_twice(
    polarised_ms: MS, options: IonosphereOptions
):
    correct_ionosphere(polarised_ms, options)
    once = read_column(polarised_ms, "IONO_CORRECTED_DATA")

    correct_ionosphere(polarised_ms, options)
    assert np.array_equal(read_column(polarised_ms, "IONO_CORRECTED_DATA"), once)
    assert np.all(read_column(polarised_ms, "DATA")[..., 0] == 2)


def test_correct_ionosphere_refuses_corrected_column(
    polarised_ms: MS, options: IonosphereOptions
):
    corrected_ms = correct_ionosphere(polarised_ms, options)
    with pytest.raises(ValueError, match="rotate the data twice"):
        correct_ionosphere(corrected_ms, options)


def test_correct_ionosphere_replaces_stale_h5parm(
    polarised_ms: MS, options: IonosphereOptions
):
    stale = correct_ionosphere(polarised_ms, options).h5parm
    correct_ionosphere(polarised_ms, options)

    with h5py.File(stale) as h5parm:
        assert list(h5parm.keys()) == ["sol000"]


def test_missing_ionex_fails_without_downloading(
    polarised_ms: MS, options: IonosphereOptions, monkeypatch
):
    def no_network(*args, **kwargs):
        raise AssertionError("Tried to download")

    monkeypatch.setattr("requests.get", no_network)
    for ionex_map in options.ionex_directory.iterdir():
        ionex_map.unlink()

    with pytest.raises(FileNotFoundError, match="flint_ionosphere download"):
        correct_ionosphere(polarised_ms, options)


def test_cli_parser(tmp_path: Path):
    args = get_parser().parse_args(["download", str(tmp_path), "a.ms", "b.ms"])
    options = create_options_from_parser(
        parser_namespace=args, options_class=IonosphereOptions
    )
    assert options.ionex_directory == tmp_path
    assert args.mss == [Path("a.ms"), Path("b.ms")]

    args = get_parser().parse_args(["correct", str(tmp_path), "a.ms", "--column", "X"])
    assert args.ms == Path("a.ms")
    assert args.column == "X"


def test_pol_flow_needs_ionex_directory(tmp_path: Path):
    from prefect.logging import disable_run_logger
    from prefect.testing.utilities import prefect_test_harness

    from flint.prefect.flows.polarisation_pipeline import process_science_fields_pol

    strategy = get_packaged_resource_path(
        package="flint.data.tests", filename="test_config_pol.yaml"
    )
    with (
        prefect_test_harness(),
        disable_run_logger(),
        pytest.raises(FileNotFoundError, match="flint_ionosphere download"),
    ):
        process_science_fields_pol(
            flint_ms_directory=tmp_path,
            pol_field_options=PolFieldOptions(
                imaging_strategy=strategy, ionex_directory=tmp_path / "missing"
            ),
            mss_by_beam=((MS(path=tmp_path / "a.ms", column="DATA"),),),
        )
