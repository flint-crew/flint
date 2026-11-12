"""Tests for creating, splitting and adjusting FITS cubes"""

from __future__ import annotations

import shutil
from functools import partial
from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from fitscube.bounding_box import get_common_bounding_box
from fitscube.extract import find_target_axis
from radio_beam import Beams

from flint.convol import BeamShape
from flint.cube import (
    combine_images_to_cube,
    rotate_cube,
    set_cube_beam,
    split_cube_into_planes,
)
from flint.exceptions import (
    NamingException,
    NotSupportedError,
    ShapeMismatchError,
)
from flint.imageset import (
    transpose_and_sort_channel_images,
)
from flint.options import FitsCubeOptions
from flint.utils import get_packaged_resource_path


def _write_channel_image(
    path: Path,
    channel: int,
    shape: tuple[int, int],
    nan_border: int = 0,
    bmaj: float = 0.01,
) -> Path:
    """A wsclean-like single channel image, with each pixel uniquely valued so
    that any scrambling of the data is detectable. A ``nan_border`` pixels wide
    border of NaNs may be added to exercise bounding-box trimming. Vary ``bmaj``
    across channels to have the cube carry a beam table."""
    ny, nx = shape
    data = (
        channel * 1000.0
        + np.arange(ny)[:, None] * 10.0
        + np.arange(nx)[None, :].astype(float)
    )
    if nan_border:
        data[:nan_border, :] = np.nan
        data[-nan_border:, :] = np.nan
        data[:, :nan_border] = np.nan
        data[:, -nan_border:] = np.nan
    header = fits.Header(
        {
            "BUNIT": "JY/BEAM",
            "BMAJ": bmaj,
            "BMIN": 0.01,
            "BPA": 0.0,
            "CTYPE1": "RA---SIN",
            "CRVAL1": 180.0,
            "CDELT1": -0.001,
            "CRPIX1": 1.0,
            "CUNIT1": "deg",
            "CTYPE2": "DEC--SIN",
            "CRVAL2": -30.0,
            "CDELT2": 0.001,
            "CRPIX2": 1.0,
            "CUNIT2": "deg",
            "CTYPE3": "FREQ",
            "CRVAL3": 8.0e8 + channel * 1.0e6,
            "CDELT3": 1.0e6,
            "CRPIX3": 1.0,
            "CUNIT3": "Hz",
            "CTYPE4": "STOKES",
            "CRVAL4": 1.0,
            "CDELT4": 1.0,
            "CRPIX4": 1.0,
            "SPECSYS": "TOPOCENT",
        }
    )
    fits.writeto(path, data=data[None, None].astype(np.float32), header=header)
    return path


def _assert_cube_matches_images(cube: Path, images: list[Path]) -> None:
    """Every channel of ``cube``, located via its own WCS, should hold the data
    and the frequency of the matching image, and the cube should be ordered
    (chan, pol, dec, ra) as linmos expects."""
    with fits.open(cube) as hdul:
        header, data = hdul[0].header, hdul[0].data

    freq_axis = find_target_axis(header=header)
    assert freq_axis.axis == header["NAXIS"]
    assert header[f"NAXIS{freq_axis.axis}"] == len(images)
    assert data.shape[0] == len(images)

    for channel, image in enumerate(images):
        plane = np.take(data, channel, axis=data.ndim - freq_axis.axis).squeeze()
        assert np.array_equal(plane, fits.getdata(image).squeeze()), channel
        expected_freq = fits.getheader(image)["CRVAL3"]
        assert freq_axis.crval + channel * freq_axis.cdelt == pytest.approx(
            expected_freq
        ), channel


def test_cube_split_and_recombine_roundtrip(tmpdir) -> None:
    """Splitting a beam cube into planes and cubing those planes again -- as the
    per-channel parallel linmos does -- must keep the data and the WCS describing
    it in step. Guards against an axis rotation not matched by the header swap."""
    tmp_path = Path(tmpdir)
    channels, shape = 3, (500, 700)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=shape,
        )
        for channel in range(channels)
    ]

    beam_cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(
            invalidate_zeros=False, remove_original_images=False
        ),
    )
    _assert_cube_matches_images(cube=beam_cube, images=images)

    planes = split_cube_into_planes(cube=beam_cube)
    for plane, image in zip(planes, images):
        assert np.array_equal(
            fits.getdata(plane).squeeze(), fits.getdata(image).squeeze()
        )

    field_cube = combine_images_to_cube(
        images=planes,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(invalidate_zeros=False),
    )
    _assert_cube_matches_images(cube=field_cube, images=images)


def test_rotate_cube_is_idempotent(tmpdir) -> None:
    """A cube already ordered (chan, pol, dec, ra) must be left alone, else the
    second pass of the per-channel linmos path would rotate it back"""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(5, 7),
        )
        for channel in range(3)
    ]
    cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(invalidate_zeros=False),
    )

    header, data = fits.getheader(cube), fits.getdata(cube)
    rotate_cube(cube)

    assert fits.getheader(cube) == header
    assert np.array_equal(fits.getdata(cube), data)


def test_combine_images_to_cube_overwrite(tmpdir) -> None:
    """An existing cube is only replaced when ``overwrite`` is set"""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(5, 7),
        )
        for channel in range(3)
    ]
    fitscube_options = FitsCubeOptions(
        invalidate_zeros=False, remove_original_images=False
    )
    combine = partial(
        combine_images_to_cube,
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
    )
    combine(fitscube_options=fitscube_options)

    with pytest.raises(FileExistsError):
        combine(fitscube_options=fitscube_options)

    cube = combine(fitscube_options=fitscube_options.with_options(overwrite=True))
    _assert_cube_matches_images(cube=cube, images=images)


def test_rotate_cube_single_stokes_is_header_only(tmpdir) -> None:
    """With a length-one stokes axis the bytes on disk do not move, so the
    rotation must be done in the header without reading the cube in."""
    cube = Path(tmpdir) / "single_stokes.fits"
    data = np.arange(1 * 3 * 5 * 7, dtype=np.float32).reshape(1, 3, 5, 7)
    header = fits.getheader(
        _write_channel_image(Path(tmpdir) / "plane.fits", channel=0, shape=(5, 7))
    )
    fits.writeto(cube, data=data, header=header)

    rotate_cube(cube)

    with fits.open(cube) as hdul:
        rotated_header, rotated_data = hdul[0].header, hdul[0].data

    assert (rotated_header["NAXIS3"], rotated_header["NAXIS4"]) == (1, 3)
    assert rotated_header["CTYPE3"] == "STOKES"
    assert rotated_header["CTYPE4"] == "FREQ"
    assert "CUNIT3" not in rotated_header
    assert rotated_header["CUNIT4"] == "Hz"
    assert np.array_equal(rotated_data, np.moveaxis(data, 1, 0))


def test_combine_images_to_cube_shape_mismatch(tmpdir) -> None:
    """Planes are written into the cube at fixed byte offsets, so differing
    pixel grids must be rejected rather than silently scrambled."""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(5, 7) if channel == 0 else (5, 6),
        )
        for channel in range(2)
    ]

    with pytest.raises(ShapeMismatchError):
        combine_images_to_cube(
            images=images,
            prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
            mode="image",
            fitscube_options=FitsCubeOptions(),
        )


def test_combine_images_to_cube_bounding_box_trims(tmpdir) -> None:
    """fitscube_options.bounding_box=True should trim the NaN border shared
    by every channel down to the smallest common valid-pixel extent."""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=2,
        )
        for channel in range(3)
    ]

    cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(bounding_box=True),
    )

    data = fits.getdata(cube)
    assert data.shape[-2:] == (6, 6)
    assert np.isfinite(data).all()


def test_combine_images_to_cube_bounding_box_override_forces_shared_grid(
    tmpdir,
) -> None:
    """A caller-supplied ``bounding_box`` must override fitscube_options.bounding_box
    so two cubes built from differently-padded inputs (e.g. image and weight
    planes) can still be forced onto an identical pixel grid."""
    tmp_path = Path(tmpdir)
    narrow_border_images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=2,
        )
        for channel in range(3)
    ]
    wide_border_images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-weight.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=1,
        )
        for channel in range(3)
    ]

    shared_box = get_common_bounding_box(file_list=narrow_border_images)

    image_cube = combine_images_to_cube(
        images=narrow_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(bounding_box=False),
        bounding_box=shared_box,
    )
    weight_cube = combine_images_to_cube(
        images=wide_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="weight",
        fitscube_options=FitsCubeOptions(
            bounding_box=False, remove_original_images=False
        ),
        bounding_box=shared_box,
    )

    # Left independent, the wide-border cube would trim to (8, 8), not (6, 6)
    independent_weight_cube = combine_images_to_cube(
        images=wide_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1.independent",
        mode="weight",
        fitscube_options=FitsCubeOptions(bounding_box=True),
    )

    assert (
        fits.getdata(image_cube).shape
        == fits.getdata(weight_cube).shape
        == (
            3,
            1,
            6,
            6,
        )
    )
    assert fits.getdata(independent_weight_cube).shape[-2:] == (8, 8)


def test_split_cube_into_planes(tmpdir) -> None:
    """A cube should split into flint named planes that can be regrouped across beams"""
    files = [
        Path(
            shutil.copy(
                get_packaged_resource_path(
                    package="flint.data.tests",
                    filename=f"SB56659.RACS_0940-04.beam17.round3-000{i}-image.sub.fits",
                ),
                Path(tmpdir),
            )
        )
        for i in range(3)
    ]
    cube = combine_images_to_cube(
        images=files,
        prefix=f"{tmpdir}/SB56659.RACS_0940-04.beam17.round3",
        mode="image",
        fitscube_options=FitsCubeOptions(),
    )

    planes = split_cube_into_planes(cube=cube)

    assert [plane.name for plane in planes] == [
        f"SB56659.RACS_0940-04.beam17.round3.i.ch{channel:04d}-{channel:04d}.fits"
        for channel in range(3)
    ]
    assert all(plane.exists() for plane in planes)
    # The names remain flint parsable, so may be regrouped per channel across beams
    assert len(transpose_and_sort_channel_images([planes, planes])) == 3

    with pytest.raises(NamingException):
        split_cube_into_planes(cube=Path(shutil.copy(cube, Path(tmpdir) / "bad.fits")))


@pytest.mark.parametrize("suffix", [".gz", ".fz"])
def test_split_cube_into_planes_refuses_compressed_cubes(tmp_path, suffix) -> None:
    """Splitting reads plane by plane through a memmap, which a compressed cube cannot give"""
    cube = tmp_path / f"SB56659.RACS_0940-04.beam17.round3.i.image.cube.fits{suffix}"
    with pytest.raises(NotSupportedError, match="compressed"):
        split_cube_into_planes(cube=cube)


def test_combine_images_to_cube_tile_compresses(tmpdir) -> None:
    """A tile-compressed cube is a .fits.fz holding the same planes"""
    files = [
        Path(
            shutil.copy(
                get_packaged_resource_path(
                    package="flint.data.tests",
                    filename=f"SB56659.RACS_0940-04.beam17.round3-000{i}-image.sub.fits",
                ),
                Path(tmpdir),
            )
        )
        for i in range(3)
    ]
    planes = [fits.getdata(file) for file in files]

    cube = combine_images_to_cube(
        images=files,
        prefix=f"{tmpdir}/SB56659.RACS_0940-04.beam17.round3",
        mode="image",
        fitscube_options=FitsCubeOptions(compress=True, compress_method="tile-gzip"),
    )

    assert cube.name.endswith(".fits.fz")
    with fits.open(cube) as hdul:
        for channel, plane in enumerate(planes):
            np.testing.assert_array_equal(
                np.squeeze(hdul[1].section[channel]), np.squeeze(plane)
            )


def _write_varying_beam_cube(tmp_path: Path, channels: int) -> Path:
    """A cube whose channels each have their own beam, so it carries a beam table"""
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(6, 6),
            bmaj=0.01 + channel * 1e-4,
        )
        for channel in range(channels)
    ]
    return combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(
            invalidate_zeros=False, remove_original_images=False
        ),
    )


def test_split_cube_into_planes_carries_channel_beams(tmp_path) -> None:
    """A plane takes its own channel's beam out of the cube's beam table, and
    stops claiming to carry a table of its own"""
    channels = 5
    cube = _write_varying_beam_cube(tmp_path=tmp_path, channels=channels)

    with fits.open(cube) as open_fits:
        assert open_fits[0].header["CASAMBM"], "Cube should carry a beam table"
        beams = Beams.from_fits_bintable(open_fits["BEAMS"])

    planes = split_cube_into_planes(cube=cube, output_path=tmp_path / "planes")

    assert len(planes) == channels
    for channel, plane in enumerate(planes):
        header = fits.getheader(plane)
        assert "CASAMBM" not in header
        assert header["BMAJ"] == pytest.approx(beams[channel].major.to(u.deg).value)
        assert header["BMIN"] == pytest.approx(beams[channel].minor.to(u.deg).value)
        assert header["BPA"] == pytest.approx(beams[channel].pa.to(u.deg).value)


def test_combine_images_to_cube_single_beam_drops_beam_table(tmp_path) -> None:
    """A cube convolved to one beam keeps it in the header, even with a blanked
    plane whose zero beam would otherwise have fitscube write a table"""
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(6, 6),
        )
        for channel in range(4)
    ]
    with fits.open(images[2], mode="update") as open_fits:
        for key in ("BMAJ", "BMIN", "BPA"):
            open_fits[0].header[key] = 0.0

    beam_shape = BeamShape(bmaj_arcsec=40.0, bmin_arcsec=30.0, bpa_deg=10.0)
    cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(
            invalidate_zeros=False, remove_original_images=False
        ),
        beam_shape=beam_shape,
    )

    with fits.open(cube) as open_fits:
        open_fits.verify("exception")
        assert len(open_fits) == 1
        header = open_fits[0].header
        data = open_fits[0].data
    assert "CASAMBM" not in header
    assert not any("beam" in str(comment) for comment in header["COMMENT"])
    assert header["BMAJ"] == pytest.approx(40.0 / 3600)
    assert header["BMIN"] == pytest.approx(30.0 / 3600)
    assert header["BPA"] == pytest.approx(10.0)
    for channel in (0, 1, 3):
        assert np.array_equal(
            data[channel].squeeze(), fits.getdata(images[channel]).squeeze()
        )
    assert np.all(np.isnan(data[2]))


def test_set_cube_beam_rewrites_when_header_grows(tmp_path) -> None:
    """A header with no room for the beam keys still gets them, and the table goes"""
    data = np.arange(16, dtype=np.float32).reshape(4, 4)
    primary = fits.PrimaryHDU(data=data)
    primary.header["CASAMBM"] = True
    # Fill the header's only block, so adding BMAJ/BMIN/BPA needs another
    while len(primary.header.tostring()) == 2880:
        primary.header.add_history("padding")
    primary.header.remove("HISTORY")
    beam_table = fits.BinTableHDU.from_columns(
        [fits.Column(name="BMAJ", format="E", array=[1.0])], name="BEAMS"
    )
    cube = tmp_path / "cube.fits"
    fits.HDUList([primary, beam_table]).writeto(cube)

    set_cube_beam(cube=cube, beam_shape=BeamShape(20.0, 10.0, 5.0))

    with fits.open(cube) as open_fits:
        assert len(open_fits) == 1
        assert "CASAMBM" not in open_fits[0].header
        assert open_fits[0].header["BMAJ"] == pytest.approx(20.0 / 3600)
        assert np.array_equal(open_fits[0].data, data)


def test_split_cube_into_planes_threaded_matches_serial(tmp_path) -> None:
    """Spreading the plane writes over threads must not change what is written"""
    cube = _write_varying_beam_cube(tmp_path=tmp_path, channels=5)

    serial = split_cube_into_planes(
        cube=cube, output_path=tmp_path / "serial", num_workers=1
    )
    threaded = split_cube_into_planes(
        cube=cube, output_path=tmp_path / "threaded", num_workers=4
    )

    assert [plane.name for plane in serial] == [plane.name for plane in threaded]
    for one, many in zip(serial, threaded):
        assert np.array_equal(fits.getdata(one), fits.getdata(many))
        assert fits.getheader(one).tostring() == fits.getheader(many).tostring()


def test_rotate_cube_no_exists() -> None:
    """Should no cube exist this should exit safely with a warning.
    This is not testing the actual rotation code, just the early
    return."""
    output_cube_path = Path("JackSparrowIsNotHere.fits")

    _ = rotate_cube(output_cube_path=output_cube_path, inplace=False)
    _ = rotate_cube(output_cube_path=output_cube_path, inplace=True)
