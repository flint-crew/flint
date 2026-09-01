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
from fitscube.extract import ExtractOptions, extract_plane_from_cube
from radio_beam import Beam, Beams
from radio_beam.utils import BeamError

from flint.convol import (
    BeamShape,
    _beams_from_cubes,
    check_if_cube_fits,
    common_beam_from_cubes,
    common_beam_shape_from_cubes,
    convolve_plane_to_beam,
    cubes_share_common_beam,
    get_cube_common_beam,
    header_beam_is_usable,
    usable_beam_mask,
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


def test_cubes_share_common_beam_with_placeholder_beams(tmp_path) -> None:
    """A blanked channel carries a placeholder rather than a resolution, so it
    neither breaks a common beam nor makes one out of cubes that have none"""
    tiny = float(np.finfo(np.float32).tiny)
    blanked = _write_cube_with_beam(tmp_path / "blanked.fits", [10.0, 0.0, tiny])
    assert cubes_share_common_beam(cube_paths=[blanked])

    all_blank = _write_cube_with_beam(tmp_path / "all_blank.fits", [0.0, tiny, 0.0])
    assert cubes_share_common_beam(cube_paths=[all_blank])

    varying = _write_cube_with_beam(tmp_path / "vary.fits", [10.0, 11.0, 0.0])
    assert not cubes_share_common_beam(cube_paths=[varying])


def test_cubes_share_common_beam_with_cutoff(tmp_path) -> None:
    """A channel coarser than the cutoff is one to blank, which only the
    convolution pass does"""
    coarse = _write_cube_with_beam(tmp_path / "coarse.fits", [10.0, 10.0, 40.0])

    assert not cubes_share_common_beam(cube_paths=[coarse], cutoff=20.0)
    # Every channel beyond the cutoff leaves nothing to convolve to
    assert cubes_share_common_beam(cube_paths=[coarse], cutoff=5.0)


def _write_plane_with_beam(path: Path, bmaj_arcsec: float | None) -> Path:
    """A single channel image, cut out of a cube exactly as
    ``split_cube_into_planes`` does, so the plane carries its channel's beam"""
    cube = _write_cube_with_beam(
        path.with_name(f"cube.{path.name}"), [bmaj_arcsec or 0.0] * 2
    )
    plane = extract_plane_from_cube(
        fits_cube=cube,
        extract_options=ExtractOptions(
            channel_index=0, output_path=path, overwrite=True
        ),
    )
    if bmaj_arcsec is None:
        with fits.open(plane, mode="update") as open_fits:
            for key in ("BMAJ", "BMIN", "BPA"):
                open_fits[0].header.pop(key, None)
    return plane


def test_usable_beam_mask() -> None:
    """The placeholders a blank channel carries are not resolutions to convolve to"""
    tiny = np.finfo(np.float32).tiny
    beams = Beams(
        major=[10.0, 0.0, tiny, 40.0, np.nan] * u.arcsec,
        minor=[8.0, 0.0, tiny, 32.0, np.nan] * u.arcsec,
        pa=[0.0, 0.0, tiny, 0.0, np.nan] * u.deg,
    )

    assert list(usable_beam_mask(beams=beams)) == [True, False, False, True, False]
    assert list(usable_beam_mask(beams=beams, cutoff=20.0)) == [
        True,
        False,
        False,
        False,
        False,
    ]


def test_header_beam_is_usable(tmp_path: Path) -> None:
    """A plane's own header is read the same way as a cube's beam table"""
    assert header_beam_is_usable(
        header=fits.getheader(_write_plane_with_beam(tmp_path / "good.fits", 10.0))
    )
    assert not header_beam_is_usable(
        header=fits.getheader(_write_plane_with_beam(tmp_path / "zero.fits", 0.0))
    )
    assert not header_beam_is_usable(
        header=fits.getheader(_write_plane_with_beam(tmp_path / "none.fits", None))
    )
    assert not header_beam_is_usable(
        header=fits.getheader(_write_plane_with_beam(tmp_path / "coarse.fits", 40.0)),
        cutoff=20.0,
    )


def test_convolve_plane_to_beam(tmp_path: Path) -> None:
    """A plane is convolved to the target beam, leaving the input in place"""
    plane = _write_plane_with_beam(tmp_path / "stokes_q.ch0000-0000.fits", 10.0)
    beam_shape = BeamShape(bmaj_arcsec=14.0, bmin_arcsec=12.0, bpa_deg=0.0)

    convolved = convolve_plane_to_beam(plane=plane, beam_shape=beam_shape)

    assert plane.exists(), "the input plane was consumed"
    assert convolved == tmp_path / "stokes_q.ch0000-0000.conv.fits"
    header = fits.getheader(convolved)
    assert header["BMAJ"] * 3600.0 == pytest.approx(14.0)
    assert header["BMIN"] * 3600.0 == pytest.approx(12.0)
    assert fits.getdata(convolved).shape == fits.getdata(plane).shape


def test_convolve_plane_to_beam_beyond_cutoff(tmp_path: Path) -> None:
    """A plane coarser than the cutoff is blanked, and marked as holding no PSF
    so the cube it is stacked into does not claim one for that channel"""
    plane = _write_plane_with_beam(tmp_path / "stokes_q.ch0000-0000.fits", 40.0)
    beam_shape = BeamShape(bmaj_arcsec=14.0, bmin_arcsec=12.0, bpa_deg=0.0)

    convolved = convolve_plane_to_beam(plane=plane, beam_shape=beam_shape, cutoff=20.0)

    assert np.all(np.isnan(fits.getdata(convolved)))
    assert fits.getheader(convolved)["BMAJ"] == 0.0


def test_convolve_plane_to_beam_without_beam(tmp_path: Path) -> None:
    """A plane carrying no PSF has no resolution to change, so it is copied through"""
    plane = _write_plane_with_beam(tmp_path / "stokes_q.ch0000-0000.fits", 0.0)
    beam_shape = BeamShape(bmaj_arcsec=14.0, bmin_arcsec=12.0, bpa_deg=0.0)

    convolved = convolve_plane_to_beam(plane=plane, beam_shape=beam_shape, cutoff=20.0)

    assert fits.getheader(convolved)["BMAJ"] == 0.0
    assert np.array_equal(fits.getdata(convolved), fits.getdata(plane))


def test_common_beam_from_cubes(tmp_path: Path) -> None:
    """The beam every channel of every cube fits inside. The spice stage sizes
    its island boxes on this, so a box holds its island in the coarsest cube
    rather than only in the finer reference image."""
    cubes = [
        _write_cube_with_beam(tmp_path / "fine.fits", [10.0, 11.0, 12.0]),
        _write_cube_with_beam(tmp_path / "coarse.fits", [14.0] * 3),
    ]

    common_beam = common_beam_from_cubes(cube_paths=cubes)

    assert common_beam is not None
    assert common_beam.major.to(u.arcsec).value >= 14.0
    # A single cube's own coarsest channel, not the set's
    single_cube_beam = common_beam_from_cubes(cube_paths=cubes[:1])
    assert single_cube_beam is not None
    assert single_cube_beam.major.to(u.arcsec).value == pytest.approx(12.0, rel=1e-3)

    # A channel beyond the cutoff is left out rather than dragging the common
    # beam out to its resolution
    cut_beam = common_beam_from_cubes(cube_paths=cubes, cutoff=13.0)
    assert cut_beam is not None
    assert cut_beam.major.to(u.arcsec).value == pytest.approx(12.0, rel=1e-2)

    # Every channel has to deconvolve the common beam to reach it, and the
    # minimum enclosing ellipse sits right against the beams it encloses, so the
    # solution is rounded up onto a tenth of an arcsecond for headroom
    for axis in (common_beam.major, common_beam.minor):
        arcsec = axis.to(u.arcsec).value
        assert arcsec == pytest.approx(round(arcsec, 1), abs=1e-6)
        assert (
            arcsec >= _beams_from_cubes(cube_paths=cubes).major.to(u.arcsec).value.min()
        )


def test_common_beam_from_cubes_without_usable_beams(tmp_path: Path) -> None:
    """Nothing but placeholder beams constrains no common beam. radio_beam
    reduces such a set to an empty sequence and raises an opaque argmax error,
    so it is never handed one."""
    tiny = float(np.finfo(np.float32).tiny)
    blank = _write_cube_with_beam(tmp_path / "blank.fits", [0.0, tiny, 0.0])

    assert common_beam_from_cubes(cube_paths=[blank]) is None
    # Nor when every real beam is beyond the cutoff
    coarse = _write_cube_with_beam(tmp_path / "coarse.fits", [40.0] * 3)
    assert common_beam_from_cubes(cube_paths=[coarse], cutoff=20.0) is None
    # Nor when there is no beam recorded at all
    no_beam = _write_cube_with_beam(tmp_path / "no_beam.fits", [10.0] * 3)
    with fits.open(no_beam, mode="update") as open_fits:
        del open_fits["BEAMS"]
        del open_fits[0].header["CASAMBM"]
    assert common_beam_from_cubes(cube_paths=[no_beam]) is None


def test_common_beam_from_cubes_retries_on_beam_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """radio_beam can fail to enclose a set of beams at its default tolerance,
    which is retried at a tenth of it rather than given up on"""
    cube = _write_cube_with_beam(tmp_path / "fine.fits", [10.0, 11.0, 12.0])
    solved = Beams.common_beam
    tolerances: list[float | None] = []

    def _fails_once(self: Beams, **kwargs: float) -> Beam:
        tolerances.append(kwargs.get("tolerance"))
        if len(tolerances) == 1:
            raise BeamError("Could not find common beam")
        return solved(self, **kwargs)

    monkeypatch.setattr(Beams, "common_beam", _fails_once)

    assert common_beam_from_cubes(cube_paths=[cube]) is not None
    assert tolerances[0] is None, "the first solve should use the defaults"
    assert tolerances[1] is not None and tolerances[1] < 1e-4


def test_common_beam_shape_from_cubes(tmp_path: Path) -> None:
    """The spice stage cannot size its island boxes without a beam, so it takes
    one that raises rather than one it has to None-check"""
    cube = _write_cube_with_beam(tmp_path / "fine.fits", [10.0, 11.0, 12.0])

    beam_shape = common_beam_shape_from_cubes(cube_paths=[cube])

    assert beam_shape.bmaj_arcsec == pytest.approx(12.0, rel=1e-2)

    tiny = float(np.finfo(np.float32).tiny)
    blank = _write_cube_with_beam(tmp_path / "blank.fits", [0.0, tiny, 0.0])
    with pytest.raises(ValueError, match="No usable restoring beam"):
        common_beam_shape_from_cubes(cube_paths=[blank])
