"""Tests around the holography hlper utility"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits

from flint.exceptions import UnusableBeamError
from flint.misc.holo import (
    FinalFITSCubeInfo,
    FITSCubeInfo,
    FrequencyGrid,
    construct_frequency_grid,
    construct_spatial_output_wcs,
    create_fits_info,
    create_placeholder_cube,
    get_freq_axis,
    get_parser,
    load_and_sort_cubes,
    load_beam_response,
    map_frequencies_to_channels,
    outside_planes,
    sample_beam_response,
)
from flint.sky_model import generate_gaussian_pb
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


# Mirrors a real ASKAP holography cube: a grid of offsets about each beam centre,
# with CRVAL at (0, 0) rather than at any field on the sky
HOLO_PIXEL_DEG = 0.0345
HOLO_FREQS_HZ = np.array([900e6, 901e6, 902e6])
FIELD_CENTRE = SkyCoord(ra=157.70551 * u.deg, dec=18.25744 * u.deg)


def _write_holo_cube(path: Path, data: np.ndarray, name: str = "holo.fits") -> Path:
    """Write a (beam, stokes, freq, dec, ra) cube with a real holography header"""
    nbeam, nstokes, nfreq, ny, nx = data.shape
    header = fits.Header()
    header["CTYPE1"] = "RA---SIN"
    header["CRVAL1"] = 0.0
    header["CDELT1"] = -HOLO_PIXEL_DEG
    header["CRPIX1"] = (nx + 1) / 2
    header["CTYPE2"] = "DEC--SIN"
    header["CRVAL2"] = 0.0
    header["CDELT2"] = HOLO_PIXEL_DEG
    header["CRPIX2"] = (ny + 1) / 2
    header["CTYPE3"] = "FREQ"
    header["CRVAL3"] = HOLO_FREQS_HZ[0]
    header["CDELT3"] = 1e6
    header["CRPIX3"] = 1.0
    header["CTYPE4"] = "STOKES"
    header["CRVAL4"] = 1.0
    header["CDELT4"] = 1.0
    header["CRPIX4"] = 1.0
    header["CTYPE5"] = "BEAM"
    header["CRVAL5"] = 0.0
    header["CDELT5"] = 1.0
    header["CRPIX5"] = 1.0

    cube_path = path / name
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(cube_path)
    return cube_path


def _make_holo_cube(path: Path) -> Path:
    """A 101x101 pixel cube with three beams.

    Beam 0 is all zeros. Beam 1 is a Gaussian peaking at 2 on the beam centre,
    with a bright marker 0.5 deg west of centre. Beam 2 is 1 within 1 deg of
    the centre and -0.1 beyond.
    """
    npix = 101
    data = np.zeros((3, 1, len(HOLO_FREQS_HZ), npix, npix))

    offsets = (np.arange(npix) - npix // 2) * HOLO_PIXEL_DEG
    xx, yy = np.meshgrid(offsets, offsets)
    radius_deg = np.hypot(xx, yy)

    data[1, 0] = 2.0 * np.exp(-4 * np.log(2) * (radius_deg / 1.5) ** 2)
    # West is +x, as CDELT1 is negative; 0.5 deg is 14.5 pixels
    data[1, 0, :, 48:53, 62:68] = 5.0
    data[2, 0] = np.where(radius_deg < 1.0, 1.0, -0.1)

    return _write_holo_cube(path=path, data=data)


def _offset_position(pa_deg: float, direction_cosine: float) -> SkyCoord:
    """A position offset from the field centre by a direction cosine radius"""
    return FIELD_CENTRE.directional_offset_by(
        pa_deg * u.deg, np.arcsin(direction_cosine) * u.rad
    )


def test_load_beam_response_normalises_to_beam_centre(tmp_path) -> None:
    """Every plane reads 1 at the beam centre"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)

    np.testing.assert_allclose(response.planes[:, 50, 50], 1.0)
    np.testing.assert_allclose(response.freqs_hz, HOLO_FREQS_HZ)
    assert response.centre_pixel == (50.0, 50.0)


def test_load_beam_response_drops_nan_planes(tmp_path) -> None:
    """All-NaN planes, as in flagged holography channels, are dropped"""
    data = fits.getdata(_make_holo_cube(tmp_path))
    data[1, 0, 0] = np.nan
    holofile = _write_holo_cube(path=tmp_path, data=data, name="edited.fits")

    response = load_beam_response(holofile=holofile, beam=1)

    np.testing.assert_allclose(response.freqs_hz, HOLO_FREQS_HZ[1:])


def test_load_beam_response_unusable_beam_raises(tmp_path) -> None:
    """A beam that is all zero or all NaN has nothing to sample"""
    holofile = _make_holo_cube(tmp_path)
    with pytest.raises(UnusableBeamError):
        load_beam_response(holofile=holofile, beam=0)

    data = fits.getdata(holofile)
    data[0] = np.nan
    holofile = _write_holo_cube(path=tmp_path, data=data, name="edited.fits")
    with pytest.raises(UnusableBeamError):
        load_beam_response(holofile=holofile, beam=0)


@pytest.mark.parametrize("crpix1", [1.0, 2.0, 101.0])
def test_load_beam_response_beam_centre_at_edge_raises(tmp_path, crpix1) -> None:
    """A beam centre within a pixel of the cube's edge leaves no room for the edge circle"""
    holofile = _make_holo_cube(tmp_path)
    fits.setval(holofile, "CRPIX1", value=crpix1)

    with pytest.raises(ValueError, match="within a pixel of its edge"):
        load_beam_response(holofile=holofile, beam=1)


def test_load_beam_response_sorts_descending_frequencies(tmp_path) -> None:
    """A cube stored with decreasing frequency comes back in increasing frequency"""
    data = np.ones((1, 1, 3, 11, 11))
    for plane in range(3):
        data[0, 0, plane, 5, 7] = plane + 1
    holofile = _write_holo_cube(path=tmp_path, data=data)
    fits.setval(holofile, "CRVAL3", value=HOLO_FREQS_HZ[-1])
    fits.setval(holofile, "CDELT3", value=-1e6)

    response = load_beam_response(holofile=holofile, beam=0)

    np.testing.assert_allclose(response.freqs_hz, HOLO_FREQS_HZ)
    np.testing.assert_allclose(response.planes[:, 5, 7], [3.0, 2.0, 1.0])


def test_outside_planes(tmp_path) -> None:
    """Only frequencies more than half a plane beyond the usable planes count as outside"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)
    freqs = np.array([899.4e6, 900e6, 902.4e6, 903e6]) * u.Hz

    outside = outside_planes(response=response, freqs=freqs)

    np.testing.assert_array_equal(outside, [True, False, False, True])


def test_sample_beam_response_field_away_from_cube_origin(tmp_path) -> None:
    """A field far from the cube's CRVAL of (0, 0) is sampled about its own centre"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)
    freqs = HOLO_FREQS_HZ * u.Hz

    at_centre = sample_beam_response(
        response=response, beam_centre=FIELD_CENTRE, position=FIELD_CENTRE, freqs=freqs
    )
    np.testing.assert_allclose(at_centre, 1.0)

    offset = sample_beam_response(
        response=response,
        beam_centre=FIELD_CENTRE,
        position=FIELD_CENTRE.directional_offset_by(180 * u.deg, 0.75 * u.deg),
        freqs=freqs,
    )
    np.testing.assert_allclose(offset, 0.5, atol=0.01)


def test_sample_beam_response_rotation_matches_linmos(tmp_path) -> None:
    """linmos looks up x = l cos(alpha) - m sin(alpha), y = l sin(alpha) + m cos(alpha),
    so at alpha = 90 deg a source north of the beam centre reads the marker to the west"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)
    freqs = HOLO_FREQS_HZ * u.Hz
    north = FIELD_CENTRE.directional_offset_by(0 * u.deg, 0.5 * u.deg)

    def sample(alpha: float) -> np.ndarray:
        return sample_beam_response(
            response=response,
            beam_centre=FIELD_CENTRE,
            position=north,
            freqs=freqs,
            alpha=alpha,
        )

    np.testing.assert_allclose(sample(np.pi / 2), 2.5)
    assert np.all(sample(0.0) < 1.0)
    assert np.all(sample(-np.pi / 2) < 1.0)


def test_sample_beam_response_off_cube_is_zero(tmp_path) -> None:
    """Without the taper, positions off the cube give no response, as in linmos"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)

    atten = sample_beam_response(
        response=response,
        beam_centre=FIELD_CENTRE,
        position=FIELD_CENTRE.directional_offset_by(30 * u.deg, 3 * u.deg),
        freqs=HOLO_FREQS_HZ * u.Hz,
    )

    np.testing.assert_array_equal(atten, 0.0)


@pytest.mark.parametrize("pa_deg, exit_pixels", [(90, 49.0), (45, 49.0 * np.sqrt(2))])
def test_sample_beam_response_taper(tmp_path, pa_deg, exit_pixels) -> None:
    """The taper takes over where the line through the source leaves the cube,
    a pixel inside its edge: continuous there, and falling off as a Gaussian beyond"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)
    freqs = HOLO_FREQS_HZ * u.Hz
    edge = exit_pixels * np.radians(HOLO_PIXEL_DEG)

    def sample(direction_cosine: float, taper: bool = True) -> np.ndarray:
        return sample_beam_response(
            response=response,
            beam_centre=FIELD_CENTRE,
            position=_offset_position(pa_deg=pa_deg, direction_cosine=direction_cosine),
            freqs=freqs,
            taper=taper,
        )

    inside, outside = sample(edge * (1 - 1e-6)), sample(edge * (1 + 1e-6))
    np.testing.assert_allclose(inside, outside, rtol=1e-3)
    assert np.all(inside > 0)

    far = 2 * edge
    falloff = (
        (
            generate_gaussian_pb(
                freqs=freqs, aperture=12 * u.m, offset=np.arcsin(far) * u.rad
            ).atten
            / generate_gaussian_pb(
                freqs=freqs, aperture=12 * u.m, offset=np.arcsin(edge) * u.rad
            ).atten
        )
        .decompose()
        .value
    )
    np.testing.assert_allclose(sample(far), outside * falloff, rtol=1e-3)
    np.testing.assert_array_equal(sample(far, taper=False), 0.0)


def test_sample_beam_response_taper_keeps_cube_corners(tmp_path) -> None:
    """With the taper on, the measured beam is still used everywhere inside the
    cube, including the corners beyond the largest circle that fits in it"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=1)
    position = _offset_position(
        pa_deg=45, direction_cosine=60 * np.radians(HOLO_PIXEL_DEG)
    )

    def sample(taper: bool) -> np.ndarray:
        return sample_beam_response(
            response=response,
            beam_centre=FIELD_CENTRE,
            position=position,
            freqs=HOLO_FREQS_HZ * u.Hz,
            taper=taper,
        )

    measured = sample(taper=False)
    assert np.all(measured > 0)
    np.testing.assert_allclose(sample(taper=True), measured)


def test_sample_beam_response_taper_negative_edge(tmp_path) -> None:
    """A response at or below zero where the line leaves the cube tapers to zero"""
    response = load_beam_response(holofile=_make_holo_cube(tmp_path), beam=2)

    atten = sample_beam_response(
        response=response,
        beam_centre=FIELD_CENTRE,
        position=_offset_position(pa_deg=45, direction_cosine=0.1),
        freqs=HOLO_FREQS_HZ * u.Hz,
        taper=True,
    )

    np.testing.assert_array_equal(atten, 0.0)
