"""Bits around testing the convolution utilities"""

from __future__ import annotations

import shutil
from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS

from flint.convol import (
    BeamShape,
    check_if_cube_fits,
    common_beam_from_cubes,
    convolve_cubes_to_common_beam,
    cubes_share_common_beam,
    get_cube_common_beam,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def image_fits() -> Path:
    image = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0-MFS-subimage_rms.fits",
        )
    )

    return image


@pytest.fixture
def cube_fits(tmpdir) -> Path:
    """Extract some example cubes"""
    tmp_dir = Path(tmpdir)
    cube_dir = Path(tmp_dir / "cubes")
    cube_dir.mkdir(parents=True, exist_ok=True)

    cubes_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests", filename="sub_cube_fits_examples.zip"
        )
    )
    assert cubes_zip.exists()
    shutil.unpack_archive(cubes_zip, cube_dir)

    return cube_dir


def test_check_if_cube_fits(cube_fits, image_fits):
    """See if the cube fits checker is picking up cubes with 3 axis"""
    fits_files = list(cube_fits.glob("*sub.fits"))
    assert len(fits_files) == 10
    assert all([check_if_cube_fits(fits_file=f) for f in fits_files])

    assert not check_if_cube_fits(fits_file=image_fits)
    assert not check_if_cube_fits(fits_file=Path("ThisDoesNotExist"))


def test_get_cube_common_beam_and_convol_cubes(cube_fits) -> None:
    """Ensure that the common beam functionality of from beamcon_3D. Also test the
    convolution to the cubes, as the initial compute can be expensive"""
    fits_files = list(cube_fits.glob("*sub.fits"))
    assert len(fits_files) == 10

    data = fits.getdata(fits_files[0])
    data_shape = np.squeeze(data).shape  # type: ignore

    beam_list = get_cube_common_beam(cube_paths=fits_files, cutoff=150.0)
    assert len(beam_list) == data_shape[0]
    assert all([isinstance(b, BeamShape) for b in beam_list])


# This can cause thread locks in testing/ Test works. Test passes but
# produces something like the below when pytest wraps up
# =============== 257 passed, 14075 warnings in 526.16s (0:08:46) ================
# Fatal Python error: _enter_buffered_busy: could not acquire lock for <_io.BufferedWriter name='<stderr>'> at interpreter shutdown, possibly due to daemon threads
# Python
# def test_beam_list_convol(cube_fits):
#     # These come from the beam_list above
#     bmaj_arcsec = [
#         14.7,
#         14.5,
#         14.3,
#         14.2,
#         14.0,
#         13.8,
#         13.8,
#         13.5,
#         13.4,
#         32.9,
#         float("nan"),
#         147.5,
#         13.0,
#         12.9,
#         12.9,
#         12.8,
#         12.7,
#         13.0,
#         40.7,
#         12.7,
#         12.3,
#         12.3,
#         12.2,
#         12.1,
#         12.0,
#         11.9,
#         11.9,
#         11.6,
#         11.4,
#         11.6,
#         11.6,
#         11.5,
#         11.4,
#         11.4,
#         11.4,
#         11.3,
#     ]
#     bmin_arcsec = [
#         12.4,
#         12.2,
#         12.1,
#         12.0,
#         11.8,
#         11.7,
#         11.7,
#         11.6,
#         11.4,
#         16.2,
#         float("nan"),
#         63.7,
#         11.0,
#         10.9,
#         10.8,
#         10.7,
#         10.6,
#         10.8,
#         16.6,
#         10.6,
#         10.3,
#         10.2,
#         10.1,
#         10.0,
#         9.9,
#         9.9,
#         9.8,
#         9.7,
#         9.3,
#         9.6,
#         9.5,
#         9.4,
#         9.4,
#         9.3,
#         9.2,
#         9.2,
#     ]
#     bpa_deg = [
#         75.25,
#         75.02,
#         74.82,
#         74.82,
#         75.0,
#         75.01,
#         75.33,
#         76.03,
#         75.43,
#         -21.79,
#         float("nan"),
#         160.53,
#         73.28,
#         73.22,
#         76.36,
#         76.14,
#         76.07,
#         74.09,
#         55.61,
#         76.84,
#         75.9,
#         75.76,
#         75.96,
#         75.1,
#         75.76,
#         75.65,
#         75.66,
#         73.47,
#         71.63,
#         75.29,
#         75.35,
#         75.69,
#         75.11,
#         75.78,
#         75.84,
#         75.89,
#     ]

#     beam_list = [
#         BeamShape(bmaj_arcsec=bmaj, bmin_arcsec=bmin, bpa_deg=bpa)
#         for bmaj, bmin, bpa in zip(bmaj_arcsec, bmin_arcsec, bpa_deg)
#     ]

#     fits_files = list(cube_fits.glob("*sub.fits"))
#     assert len(fits_files) == 10
#     # This appears to make pytest lock up

#     cube_paths = convolve_cubes(
#         cube_paths=fits_files,
#         beam_shapes=beam_list,
#         cutoff=150.0,
#         executor_type="process",
#     )
#     assert all([isinstance(p, Path) for p in cube_paths])
#     assert all([p.exists() for p in cube_paths])


def _write_cube_with_beam(path: Path, bmaj_arcsec: list[float]) -> Path:
    """Write a small ASKAP-shaped (chan, stokes, dec, ra) cube whose channels
    carry the requested major axes"""
    n_chan = len(bmaj_arcsec)
    freq_hz = np.linspace(700e6, 1300e6, n_chan)

    wcs = WCS(naxis=4)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "STOKES", "FREQ"]
    wcs.wcs.crval = [180.0, -30.0, 1.0, freq_hz[0]]
    wcs.wcs.crpix = [8, 8, 1, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, 1.0, freq_hz[1] - freq_hz[0]]
    wcs.wcs.cunit = ["deg", "deg", "", "Hz"]

    rng = np.random.default_rng(0)
    primary = fits.PrimaryHDU(
        data=rng.normal(0, 1e-3, (n_chan, 1, 16, 16)).astype(np.float32),
        header=wcs.to_header(),
    )
    primary.header["BUNIT"] = "Jy/beam"
    primary.header["CASAMBM"] = True

    beam_table = fits.table_to_hdu(
        Table(
            data=[
                np.array(bmaj_arcsec, dtype="f4"),
                np.array(bmaj_arcsec, dtype="f4") * 0.8,
                np.zeros(n_chan, dtype="f4"),
                np.arange(n_chan, dtype="i4"),
                np.zeros(n_chan, dtype="i4"),
            ],
            names=["BMAJ", "BMIN", "BPA", "CHAN", "POL"],
            units=["arcsec", "arcsec", "deg", None, None],
        )
    )
    beam_table.header["EXTNAME"] = "BEAMS"

    fits.HDUList([primary, beam_table]).writeto(path, overwrite=True)
    return path


def test_cubes_share_common_beam(tmp_path) -> None:
    """Cubes only share a resolution when every channel of every cube matches"""
    matching = [
        _write_cube_with_beam(tmp_path / f"match_{pol}.fits", [10.0] * 3)
        for pol in ("q", "u")
    ]
    assert cubes_share_common_beam(cube_paths=matching)

    varying = _write_cube_with_beam(tmp_path / "vary.fits", [10.0, 11.0, 12.0])
    assert not cubes_share_common_beam(cube_paths=[*matching, varying])

    # Differing only across Stokes is enough to need a convolution
    offset = _write_cube_with_beam(tmp_path / "offset.fits", [10.5] * 3)
    assert not cubes_share_common_beam(cube_paths=[matching[0], offset])


def test_cubes_share_common_beam_without_beams(tmp_path) -> None:
    """A cube carrying no beam information has no resolution to make common"""
    cube = _write_cube_with_beam(tmp_path / "no_beam.fits", [10.0] * 3)
    with fits.open(cube, mode="update") as open_fits:
        del open_fits["BEAMS"]
        del open_fits[0].header["CASAMBM"]

    assert cubes_share_common_beam(cube_paths=[cube])


def test_convolve_cubes_to_common_beam(tmp_path) -> None:
    """Convolving leaves the input cubes alone, writes new cubes on the same
    pixel grid, and brings every channel of every cube to the one beam"""
    cubes = [
        _write_cube_with_beam(
            tmp_path / f"stokes_{pol}.fits",
            [10.0 + offset, 11.0 + offset, 12.0 + offset],
        )
        for pol, offset in (("q", 0.0), ("u", 0.5))
    ]
    output_path = tmp_path / "rmsynth"

    convolved = convolve_cubes_to_common_beam(cube_paths=cubes, output_path=output_path)

    assert all(cube.exists() for cube in cubes), "the input cubes were consumed"
    assert not set(convolved) & set(cubes)
    assert all(cube.parent == output_path for cube in convolved)
    # The outputs must pair back to their inputs, which smooth_fits_cube sorts
    assert [cube.name for cube in convolved] == [
        "stokes_q.conv.fits",
        "stokes_u.conv.fits",
    ]
    for cube, convolved_cube in zip(cubes, convolved):
        assert fits.getdata(convolved_cube).shape == fits.getdata(cube).shape
    assert cubes_share_common_beam(cube_paths=convolved)


def test_common_beam_from_cubes(tmp_path: Path) -> None:
    """The beam every channel of every cube fits inside. The spice stage sizes
    its island boxes on this, so a box holds its island in the coarsest cube
    rather than only in the finer reference image."""
    cubes = [
        _write_cube_with_beam(tmp_path / "fine.fits", [10.0, 11.0, 12.0]),
        _write_cube_with_beam(tmp_path / "coarse.fits", [14.0] * 3),
    ]

    common_beam = common_beam_from_cubes(cube_paths=cubes)

    assert common_beam.major.to(u.arcsec).value >= 14.0
    # A single cube's own coarsest channel, not the set's
    assert common_beam_from_cubes(cube_paths=cubes[:1]).major.to(
        u.arcsec
    ).value == pytest.approx(12.0, rel=1e-3)


def test_repeated_convolution_resets_the_racs_tools_log_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """racs_tools starts its module-level log listener on every smooth_fits_cube
    call and only enqueues its sentinel, so the finished thread stays recorded.
    Python 3.13 rejects the next start over that, which took out every
    convolving test after the first. Only 3.13 raises, so the guard is installed
    here to cover the same ground on 3.11/3.12."""
    from logging.handlers import QueueListener

    from racs_tools import beamcon_3D

    start = QueueListener.start

    def start_once(self: QueueListener) -> None:
        if getattr(self, "_thread", None) is not None:
            raise RuntimeError("Listener already started")
        start(self)

    monkeypatch.setattr(QueueListener, "start", start_once)

    cubes = [
        _write_cube_with_beam(tmp_path / "reset_q.fits", [10.0] * 3),
        _write_cube_with_beam(tmp_path / "reset_u.fits", [11.0] * 3),
    ]

    for output_name in ("first", "second"):
        convolve_cubes_to_common_beam(
            cube_paths=cubes, output_path=tmp_path / output_name
        )
        assert beamcon_3D.log_listener._thread is None
        # A leftover sentinel would kill the next listener as it starts, which
        # is why the finished thread is joined rather than stopped
        assert beamcon_3D.log_queue.empty()

    # The order flint_convol --mode convol --cubes runs in: the dry run sends no
    # sentinel of its own, so there stop() is needed
    get_cube_common_beam(cube_paths=cubes)
    assert beamcon_3D.log_listener._thread is None
    convolve_cubes_to_common_beam(cube_paths=cubes, output_path=tmp_path / "third")
