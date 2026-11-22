"""flint on fitscube-rs and convolve-rs against fitscube and racs_tools, the
Python packages they replace, on flint's own test data."""

from __future__ import annotations

import shutil
from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from fitscube_rs import compat

from flint.convol import (
    BeamShape,
    convolve_cubes,
    convolve_images,
    get_common_beam,
    get_cube_common_beam,
)
from flint.utils import get_packaged_resource_path

fitscube_combine = pytest.importorskip("fitscube.combine_fits")
fitscube_bbox = pytest.importorskip("fitscube.bounding_box")
beamcon_2D = pytest.importorskip("racs_tools.beamcon_2D")
beamcon_3D = pytest.importorskip("racs_tools.beamcon_3D")
radio_beam = pytest.importorskip("radio_beam")

# The two minimum-volume-ellipse fits differ slightly; both round up to 0.1"
# and 0.01 deg, so the rounded axes can differ by one step.
BEAM_ATOL = (0.11, 0.11, 0.5)


@pytest.fixture
def channel_images(tmp_path: Path) -> list[Path]:
    """flint's three wsclean channel images, copied so outputs land in tmp_path."""
    paths = []
    for channel in range(3):
        name = f"SB56659.RACS_0940-04.beam17.round3-{channel:04d}-image.sub.fits"
        src = Path(get_packaged_resource_path("flint.data.tests", name))
        paths.append(Path(shutil.copy(src, tmp_path / name)))
    return paths


@pytest.fixture
def cubes(tmp_path: Path) -> list[Path]:
    """flint's example cubes."""
    shutil.unpack_archive(
        get_packaged_resource_path("flint.data.tests", "sub_cube_fits_examples.zip"),
        tmp_path / "cubes",
    )
    return sorted((tmp_path / "cubes").glob("*sub.fits"))


def _assert_beams_close(ours: BeamShape, theirs: BeamShape) -> None:
    """Axes within a rounding step; position angles equal modulo 180 deg."""
    assert ours.bmaj_arcsec == pytest.approx(theirs.bmaj_arcsec, abs=BEAM_ATOL[0])
    assert ours.bmin_arcsec == pytest.approx(theirs.bmin_arcsec, abs=BEAM_ATOL[1])
    pa = (ours.bpa_deg - theirs.bpa_deg + 90) % 180 - 90
    assert abs(pa) <= BEAM_ATOL[2], (ours, theirs)


def _assert_images_close(ours: np.ndarray, theirs: np.ndarray) -> None:
    """The tolerances of convolve-rs's racs_tools parity tests."""
    assert np.isnan(ours)[np.isnan(theirs)].all()
    good = ~np.isnan(ours)
    scale = np.nanstd(theirs) or 1.0
    np.testing.assert_allclose(ours[good], theirs[good], rtol=0, atol=1e-4 * scale)


def test_combine_fits(channel_images, tmp_path):
    ours = compat.combine_fits(channel_images, tmp_path / "rs.fits")
    theirs = fitscube_combine.combine_fits(channel_images, tmp_path / "py.fits")
    np.testing.assert_allclose(ours.to_value(u.Hz), theirs.to_value(u.Hz))
    np.testing.assert_array_equal(
        fits.getdata(tmp_path / "rs.fits"), fits.getdata(tmp_path / "py.fits")
    )


def test_even_spacing():
    freqs = [8.0849e8, 8.2649e8, 8.6249e8] * u.Hz
    ours, theirs = compat.even_spacing(freqs), fitscube_combine.even_spacing(freqs)
    np.testing.assert_allclose(ours.specs.value, theirs.specs.value)
    np.testing.assert_array_equal(ours.missing_chan_idx, theirs.missing_chan_idx)


def test_get_common_bounding_box(channel_images):
    for path in channel_images:
        with fits.open(path, mode="update") as hdul:
            hdul[0].data[..., :2, :] = np.nan
            hdul[0].data[..., :, -3:] = np.nan
    ours = compat.get_common_bounding_box(channel_images)
    theirs = fitscube_bbox.get_common_bounding_box(channel_images)
    fields = ("xmin", "xmax", "ymin", "ymax")
    assert [getattr(ours, f) for f in fields] == [getattr(theirs, f) for f in fields]


def test_get_common_beam(channel_images):
    ours = get_common_beam(channel_images)
    beam, _ = beamcon_2D.get_common_beam(files=channel_images)
    _assert_beams_close(ours, BeamShape.from_radio_beam(beam))


def test_convolve_images(channel_images, tmp_path):
    target = BeamShape(bmaj_arcsec=25.0, bmin_arcsec=20.0, bpa_deg=110.0)
    ours = convolve_images(channel_images, beam_shape=target, convol_suffix="rs")
    radio_target = radio_beam.Beam(25.0 * u.arcsec, 20.0 * u.arcsec, 110.0 * u.deg)
    for image, out in zip(channel_images, ours, strict=True):
        beamcon_2D.beamcon_2d_on_fits(
            file=image,
            outdir=None,
            new_beam=radio_target,
            conv_mode="robust",
            suffix="py",
        )
        theirs = fits.getdata(image.with_suffix(".py.fits"))
        _assert_images_close(fits.getdata(out), theirs)
        assert fits.getheader(out)["BMAJ"] == pytest.approx(25.0 / 3600)


def test_get_cube_common_beam(cubes):
    ours = get_cube_common_beam(cubes, cutoff=150.0)
    _, common = beamcon_3D.smooth_fits_cube(
        infiles_list=cubes,
        dryrun=True,
        cutoff=150.0,
        mode="natural",
        conv_mode="robust",
        ncores=1,
    )
    theirs = [BeamShape.from_radio_beam(b) for b in common[0][0]]
    assert len(ours) == len(theirs)
    for a, b in zip(ours, theirs, strict=True):
        if np.isnan(b.bmaj_arcsec):
            assert np.isnan(a.bmaj_arcsec)
        else:
            _assert_beams_close(a, b)


def test_convolve_cubes(cubes):
    cubes = cubes[:2]
    targets = get_cube_common_beam(cubes, cutoff=150.0)
    ours = convolve_cubes(cubes, beam_shapes=targets, cutoff=150.0, convol_suffix="rs")
    beamcon_3D.smooth_fits_cube(
        infiles_list=cubes,
        dryrun=False,
        cutoff=150.0,
        mode="natural",
        conv_mode="robust",
        bmaj=[b.bmaj_arcsec for b in targets],
        bmin=[b.bmin_arcsec for b in targets],
        bpa=[b.bpa_deg for b in targets],
        suffix="py",
        ncores=1,
    )
    for cube, out in zip(cubes, ours, strict=True):
        _assert_images_close(
            fits.getdata(out), fits.getdata(cube.with_suffix(".py.fits"))
        )
