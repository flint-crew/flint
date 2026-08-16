"""Tests around the holography hlper utility"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs.utils import pixel_to_skycoord

from flint.misc.holo import (
    FinalFITSCubeInfo,
    FITSCubeInfo,
    FrequencyGrid,
    celestial_wcs_from_header,
    construct_frequency_grid,
    construct_spatial_output_wcs,
    create_fits_info,
    create_placeholder_cube,
    get_freq_axis,
    get_parser,
    load_and_sort_cubes,
    map_frequencies_to_channels,
    sample_beam_attenuation,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def example_cube_fits(tmpdir) -> tuple[Path, ...]:
    """Load and unzip example cube data"""

    cube_zip = get_packaged_resource_path(
        package="flint.data.tests", filename="test_holo_cubes.zip"
    )

    output_path = Path(tmpdir) / "subset_cubes"
    output_path.mkdir(parents=True, exist_ok=True)

    shutil.unpack_archive(cube_zip, output_path)

    output_cubes = sorted(tuple(output_path.glob("*fits")))
    assert len(output_cubes) == 3

    return tuple(output_cubes)


def test_map_frequencies_to_channels() -> None:
    """Resolve the matching to frequencies between sets and where
    they insert into"""
    # Base range goes from 800 (inclusive) to 1800 (exclusive)
    freqs_1 = np.arange(1000) + 800
    freqs_2 = np.arange(200) + 800

    ch_out, ch_in = map_frequencies_to_channels(
        freqs_1=freqs_1, freqs_2=freqs_2, tol=1e-6
    )
    assert ch_out[0] == 0
    assert ch_out[-1] == 199
    assert ch_in[0] == 0
    assert ch_in[-1] == 199

    freqs_3 = np.arange(200) + 1000
    ch_out, ch_in = map_frequencies_to_channels(
        freqs_1=freqs_1, freqs_2=freqs_3, tol=1e-6
    )
    assert ch_out[0] == 200
    assert ch_out[-1] == 399
    assert ch_in[0] == 0
    assert ch_in[-1] == 199

    freqs_4 = np.arange(200) + 1700
    ch_out, ch_in = map_frequencies_to_channels(
        freqs_1=freqs_1, freqs_2=freqs_4, tol=1e-6
    )
    assert ch_out[0] == 900
    assert ch_out[-1] == 999
    assert ch_in[0] == 0
    assert ch_in[-1] == 99


def test_construct_placeholder_cube(example_cube_fits, tmpdir) -> None:
    """Make sure the inplace cube can be formed"""
    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    hdr = construct_spatial_output_wcs(fits_cube_infos=fits_cube_infos)
    frequency_grid = construct_frequency_grid(fits_cube_infos=fits_cube_infos)

    output_path = Path(tmpdir) / "example_concat.fits"

    cube = create_placeholder_cube(
        fits_cube_infos=fits_cube_infos,
        spatial_header=hdr,
        frequency_grid=frequency_grid,
        output_path=output_path,
    )

    assert isinstance(cube, FinalFITSCubeInfo)
    assert cube.path == output_path
    new_hdr = fits.getheader(cube.path)
    assert new_hdr["NAXIS"] == 5
    assert len(cube.output_shape) == 5
    assert cube.data_offset == 5760
    d = fits.getdata(cube.path)
    assert len(d.shape) == 5
    assert d.shape == tuple(cube.output_shape)


def test_construct_frequency_grid(example_cube_fits) -> None:
    """Construct the frequnecy grid that would be placed into the
    final output cube"""
    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    frequency_grid = construct_frequency_grid(fits_cube_infos=fits_cube_infos)
    assert isinstance(frequency_grid, FrequencyGrid)
    assert frequency_grid.cdelt == 1000000.0
    assert frequency_grid.min_hz == 799990740.7407407
    assert frequency_grid.max_hz == 1798990740.740741
    assert len(frequency_grid.grid) == 1000


def test_get_freq_axis(example_cube_fits) -> None:
    """Extract the spectral axis from a cube and reconstruct the frequency axis
    as a numpy array, in herta"""
    header = fits.getheader(example_cube_fits[0])

    freqs = get_freq_axis(header=header)
    print(example_cube_fits[0])

    assert freqs[0] == 1151990740.7407405
    assert freqs[19] == 1170990740.7407405
    assert len(freqs) == 288


def test_create_spatual_wcs(example_cube_fits) -> None:
    """Construct the final WCS 2d axis"""
    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    hdr = construct_spatial_output_wcs(fits_cube_infos=fits_cube_infos)
    assert isinstance(hdr, fits.Header)

    assert np.isclose(hdr["CRVAL1"], 6.6871180879046e-21)
    assert np.isclose(hdr["CDELT1"], -0.018502035580794)


def test_load_and_sort_cubes(example_cube_fits) -> None:
    """Test the loader and sorter that forms the FITSCubeInfos"""

    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    assert isinstance(fits_cube_infos, list)
    assert len(fits_cube_infos) == 3
    assert all(
        isinstance(fits_cube_info, FITSCubeInfo) for fits_cube_info in fits_cube_infos
    )
    # makes sure sorted in ascending order
    assert np.all(
        np.diff([fits_cube_info.freqs_hz[0] for fits_cube_info in fits_cube_infos]) > 0
    )


def test_make_fits_cube_info(example_cube_fits) -> None:
    """Construct an example FITSCubeInfo instance"""

    fits_cube_info = create_fits_info(cube_path=example_cube_fits[0])

    assert isinstance(fits_cube_info, FITSCubeInfo)
    assert fits_cube_info.spatial_shape == (3, 3)


def test_get_parser() -> None:
    """A dumb tester for the argument parser"""
    _ = get_parser()


def _make_holo_cube(path: Path) -> Path:
    """A small synthetic 5D (beam, stokes, freq, dec, ra) holography cube,
    with beam 1's Stokes I response peaking at the reference pixel and
    decreasing linearly with frequency."""
    nbeam, nstokes, nfreq, ny, nx = 2, 2, 4, 5, 5
    data = np.zeros((nbeam, nstokes, nfreq, ny, nx), dtype=np.float32)
    for chan in range(nfreq):
        data[1, 0, chan, 2, 2] = 1.0 - 0.1 * chan

    # Mirrors the keys/ordering of a real ASKAP holography cube header
    header = fits.Header()
    header["SIMPLE"] = True
    header["BITPIX"] = -32
    header["NAXIS"] = 5
    header["NAXIS1"] = nx
    header["NAXIS2"] = ny
    header["NAXIS3"] = nfreq
    header["NAXIS4"] = nstokes
    header["NAXIS5"] = nbeam
    header["EXTEND"] = True
    header["CTYPE1"] = "RA---SIN"
    header["CRVAL1"] = 180.0
    header["CDELT1"] = -0.01
    header["CRPIX1"] = 3.0
    header["CTYPE2"] = "DEC--SIN"
    header["CRVAL2"] = -30.0
    header["CDELT2"] = 0.01
    header["CRPIX2"] = 3.0
    header["CTYPE3"] = "FREQ"
    header["CRVAL3"] = 900e6
    header["CDELT3"] = 1e6
    header["CRPIX3"] = 1.0
    header["CTYPE4"] = "STOKES"
    header["CRPIX4"] = 1.0
    header["CDELT4"] = 1.0
    header["CRVAL4"] = 1.0
    header["CTYPE5"] = "BEAM"
    header["CRVAL5"] = 0.0
    header["CDELT5"] = 1.0
    header["CRPIX5"] = 1.0
    header["EQUINOX"] = 2000.0
    header["RADESYS"] = "FK5"
    header["LONPOLE"] = 180.0
    header["LATPOLE"] = 0.0
    header["SPECSYS"] = "TOPOCENT"

    cube_path = path / "holo.fits"
    fits.PrimaryHDU(data=data, header=header).writeto(cube_path)
    return cube_path


def test_sample_beam_attenuation(tmp_path) -> None:
    """Interpolate a holography cube's own frequency grid onto arbitrary freqs"""
    holofile = _make_holo_cube(tmp_path)
    position = SkyCoord(ra=180.0 * u.deg, dec=-30.0 * u.deg)
    freqs = np.array([900e6, 901e6, 902e6, 903e6]) * u.Hz

    atten = sample_beam_attenuation(
        holofile=holofile, beam=1, position=position, freqs=freqs
    )

    # bilinear, so a tiny sub-pixel offset from the WCS solve is expected
    np.testing.assert_allclose(atten, [1.0, 0.9, 0.8, 0.7], atol=1e-2)


def test_sample_beam_attenuation_outside_cube_raises(tmp_path) -> None:
    """A position well outside the cube's small footprint should raise"""
    holofile = _make_holo_cube(tmp_path)
    position = SkyCoord(ra=180.0 * u.deg, dec=10.0 * u.deg)
    freqs = np.array([900e6]) * u.Hz

    with pytest.raises(ValueError):
        sample_beam_attenuation(
            holofile=holofile, beam=1, position=position, freqs=freqs
        )


def _make_offset_holo_cube(path: Path) -> Path:
    """Single-beam cube with its bright pixel offset one pixel along RA from
    the tangent point (rather than sitting on it), so a rotation of the query
    position is actually detectable."""
    nbeam, nstokes, nfreq, ny, nx = 1, 1, 1, 5, 5
    data = np.zeros((nbeam, nstokes, nfreq, ny, nx), dtype=np.float32)
    data[0, 0, 0, 2, 3] = 1.0  # one pixel from the tangent point (2, 2), along RA

    header = fits.Header()
    header["SIMPLE"] = True
    header["BITPIX"] = -32
    header["NAXIS"] = 5
    header["NAXIS1"] = nx
    header["NAXIS2"] = ny
    header["NAXIS3"] = nfreq
    header["NAXIS4"] = nstokes
    header["NAXIS5"] = nbeam
    header["EXTEND"] = True
    header["CTYPE1"] = "RA---SIN"
    header["CRVAL1"] = 180.0
    header["CDELT1"] = -0.01
    header["CRPIX1"] = 3.0
    header["CTYPE2"] = "DEC--SIN"
    header["CRVAL2"] = -30.0
    header["CDELT2"] = 0.01
    header["CRPIX2"] = 3.0
    header["CTYPE3"] = "FREQ"
    header["CRVAL3"] = 900e6
    header["CDELT3"] = 1e6
    header["CRPIX3"] = 1.0
    header["CTYPE4"] = "STOKES"
    header["CRPIX4"] = 1.0
    header["CDELT4"] = 1.0
    header["CRVAL4"] = 1.0
    header["CTYPE5"] = "BEAM"
    header["CRVAL5"] = 0.0
    header["CDELT5"] = 1.0
    header["CRPIX5"] = 1.0
    header["EQUINOX"] = 2000.0
    header["RADESYS"] = "FK5"
    header["LONPOLE"] = 180.0
    header["LATPOLE"] = 0.0
    header["SPECSYS"] = "TOPOCENT"

    cube_path = path / "holo_offset.fits"
    fits.PrimaryHDU(data=data, header=header).writeto(cube_path)
    return cube_path


def test_sample_beam_attenuation_alpha_rotates_position(tmp_path) -> None:
    """A field rotated relative to the holography needs its query position
    de-rotated about the cube's tangent point before sampling (mirrors the
    rotation linmos applies via ASKAP_PB.alpha) -- a 90 degree differential
    rotation should move the sampled response away from the source's own
    (un-rotated) bright pixel"""
    holofile = _make_offset_holo_cube(tmp_path)
    wcs = celestial_wcs_from_header(fits.getheader(holofile))
    position = pixel_to_skycoord(3, 2, wcs=wcs, origin=0)
    freqs = np.array([900e6]) * u.Hz

    atten_no_rotation = sample_beam_attenuation(
        holofile=holofile, beam=0, position=position, freqs=freqs, alpha=0.0
    )
    assert np.isclose(atten_no_rotation[0], 1.0, atol=1e-2)

    atten_rotated = sample_beam_attenuation(
        holofile=holofile, beam=0, position=position, freqs=freqs, alpha=np.pi / 2
    )
    assert atten_rotated[0] < 0.1
