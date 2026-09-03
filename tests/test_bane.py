"""Tests for the FFT BANE port in ``flint.bane``"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from flint.bane import (
    FFTBANEOptions,
    bane_fits_image,
    fft_average,
    gaussian_kernel,
    get_kernel,
    pad_reflect,
    robust_bane,
    tophat_kernel,
)

NY = NX = 1024
PIX_PER_BEAM = 10


def _header(shape: tuple[int, int] = (NY, NX)) -> fits.Header:
    """A minimal header carrying the beam and pixel scale ``get_kernel`` needs"""
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN"]
    wcs.wcs.crval = [180.0, -30.0]
    wcs.wcs.crpix = [shape[1] / 2, shape[0] / 2]
    wcs.wcs.cdelt = [-1 / 3600, 1 / 3600]
    wcs.wcs.cunit = ["deg", "deg"]
    header = wcs.to_header()
    header["BMAJ"] = PIX_PER_BEAM / 3600
    header["BMIN"] = PIX_PER_BEAM / 3600
    header["BPA"] = 0.0
    return header


def _sky(background: float = 0.0, rms: float = 1e-3, seed: int = 0) -> np.ndarray:
    """Noise on a flat background, with three point sources"""
    rng = np.random.default_rng(seed)
    image = rng.normal(background, rms, (NY, NX)).astype(np.float32)
    for y, x in ((200, 300), (700, 800), (500, 120)):
        image[y, x] += 0.5
    return image


def test_pad_reflect_matches_numpy() -> None:
    """It is a numba-compatible np.pad(mode='reflect'), so it has to agree with
    the thing it replaces."""
    rng = np.random.default_rng(0)
    array = rng.normal(size=(17, 23)).astype(np.float32)

    for pad_y, pad_x in ((1, 1), (5, 3), (8, 11)):
        assert np.allclose(
            pad_reflect(array, (pad_y, pad_x)),
            np.pad(array, ((pad_y, pad_y), (pad_x, pad_x)), mode="reflect"),
        ), (pad_y, pad_x)


def test_fft_average_preserves_a_flat_image() -> None:
    """The kernel is normalised by its own sum, so smoothing a constant image
    returns that constant rather than scaling it."""
    image = np.full((128, 128), 3.0, dtype=np.float32)

    for kernel in (gaussian_kernel(5), tophat_kernel(8)):
        assert np.allclose(fft_average(image, kernel), 3.0, atol=1e-4)


def test_bane_recovers_a_varying_background_and_rms() -> None:
    """The point of BANE: both the background and the noise vary across the
    image, and a single number for either would be wrong. Neither is uniform
    here, and the sources must not drag the noise up."""
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:NY, 0:NX]
    truth_bkg = 2e-3 * (yy / NY)
    truth_rms = 1e-3 * (1 + xx / NX)
    image = (rng.normal(0, 1, (NY, NX)) * truth_rms + truth_bkg).astype(np.float32)
    image[200, 300] += 0.5

    background, rms = robust_bane(image=image, header=_header())

    assert background.shape == image.shape
    assert rms.shape == image.shape
    assert np.isfinite(background).all()
    assert np.isfinite(rms).all()

    # Downsampling and smoothing bias both low, so this is a loose check that
    # the maps track the truth rather than a tight one on their values
    assert np.nanmedian(background) == pytest.approx(np.median(truth_bkg), rel=0.3)
    assert np.nanmedian(rms) == pytest.approx(np.median(truth_rms), rel=0.3)

    # The gradient is the part a scalar noise estimate cannot express
    assert np.nanmedian(rms[:, -100:]) > 1.5 * np.nanmedian(rms[:, :100])
    assert np.nanmedian(background[-100:, :]) > np.nanmedian(background[:100, :])


def test_blanked_pixels_stay_blank() -> None:
    """A linmos mosaic blanks beyond its cutoff. Those pixels have no data to
    measure, so they come back NaN rather than as an extrapolated background."""
    image = _sky()
    blank = np.zeros_like(image, dtype=bool)
    blank[:50, :] = True
    image[blank] = np.nan

    background, rms = robust_bane(image=image, header=_header())

    assert np.isnan(background[blank]).all()
    assert np.isnan(rms[blank]).all()
    assert np.isfinite(background[~blank]).all()
    assert np.isfinite(rms[~blank]).all()


def _footprint(radius: int) -> np.ndarray:
    """Mask of a circular mosaic footprint, as linmos leaves after its cutoff"""
    yy, xx = np.mgrid[0:NY, 0:NX]
    return (yy - NY / 2) ** 2 + (xx - NX / 2) ** 2 < radius**2


def test_the_linmos_zero_fill_is_treated_as_blank() -> None:
    """linmos fills beyond its cutoff with exact zeros, not NaNs. Counted as
    measured data those zeros take the seed median and mad_std to exactly zero
    once more than half the plane is blank, and the maps collapse to zero
    everywhere, which reads downstream as a noiseless image."""
    inside = _footprint(radius=380)
    assert (~inside).mean() > 0.5, "the collapse needs over half the plane blank"

    sky = _sky(background=5e-4, rms=1e-3)
    zero_filled = np.where(inside, sky, 0.0).astype(np.float32)
    nan_filled = np.where(inside, sky, np.nan).astype(np.float32)

    zero_bkg, zero_rms = robust_bane(image=zero_filled, header=_header())
    nan_bkg, nan_rms = robust_bane(image=nan_filled, header=_header())

    # The noise inside the footprint was measured, not zeroed
    assert np.nanmedian(zero_rms[inside]) == pytest.approx(1e-3, rel=0.3)
    # Blanking either way describes the same plane, so it measures the same
    assert np.nanmedian(zero_rms[inside]) == pytest.approx(
        np.nanmedian(nan_rms[inside]), rel=0.05
    )
    assert np.nanmedian(zero_bkg[inside]) == pytest.approx(
        np.nanmedian(nan_bkg[inside]), rel=0.05
    )

    # And the maps blank where the image is blank, so the noise cube and the
    # image cube it describes share a footprint
    assert np.isnan(zero_rms[~inside]).all()
    assert np.isnan(zero_bkg[~inside]).all()


def test_invalidate_zeros_can_be_turned_off() -> None:
    """Off, zeros count as measured data again, which is what a caller whose
    zeros are real would want."""
    inside = _footprint(radius=380)
    zero_filled = np.where(inside, _sky(rms=1e-3), 0.0).astype(np.float32)

    _, rms = robust_bane(
        image=zero_filled,
        header=_header(),
        fft_bane_options=FFTBANEOptions(invalidate_zeros=False),
    )

    # Nothing is blank, so nothing is NaN, and the collapse described above is
    # exactly what the zeros produce
    assert np.isfinite(rms).all()
    assert np.all(rms == 0.0)


def test_the_rms_map_is_never_negative() -> None:
    """The maps are zoomed back up with a cubic spline, which rings across the
    step at a footprint edge and undershoots below zero. A negative noise
    squares to a small variance, so an inverse-variance weight built from it
    comes out orders of magnitude too large rather than obviously wrong."""
    inside = _footprint(radius=480)
    sky = _sky(rms=1e-3)

    for filled in (
        np.where(inside, sky, np.nan).astype(np.float32),
        np.where(inside, sky, 0.0).astype(np.float32),
    ):
        _, rms = robust_bane(image=filled, header=_header())
        assert not np.any(rms[np.isfinite(rms)] < 0.0)


def test_the_seed_makes_a_rerun_reproducible() -> None:
    """Clipped source pixels are refilled with random noise, so without a fixed
    seed the same image gives different maps run to run."""
    image = _sky()
    # A cut low enough that the refilled pixels survive the downsampling; at the
    # default 5 sigma only a handful of pixels are touched and the maps agree
    # whatever the seed
    options = FFTBANEOptions(clip_sigma=1.0)

    first, _ = robust_bane(image=image, header=_header(), fft_bane_options=options)
    again, _ = robust_bane(image=image, header=_header(), fft_bane_options=options)
    assert np.array_equal(first, again)

    other, _ = robust_bane(
        image=image,
        header=_header(),
        fft_bane_options=options.with_options(seed=99),
    )
    assert not np.array_equal(first, other)


def test_get_kernel_sizes_itself_from_the_beam() -> None:
    """Unset sizes come from the restoring beam, at 3 and 10 beams. A negative
    value keeps that behaviour but sets the beam count."""
    kernel, step = get_kernel(header=_header())
    assert step == 3 * PIX_PER_BEAM
    assert kernel.max() == pytest.approx(1.0)

    _, step = get_kernel(header=_header(), step_size=-5)
    assert step == 5 * PIX_PER_BEAM

    kernel, step = get_kernel(header=_header(), step_size=7, box_size=4)
    assert step == 7
    assert kernel.shape == gaussian_kernel(4).shape


def test_get_kernel_needs_a_beam_it_can_read() -> None:
    """Without a beam there is nothing to size the kernel against, so it says so
    rather than picking a number."""
    header = _header()
    for key in ("BMAJ", "BMIN", "BPA"):
        del header[key]

    with pytest.raises(ValueError, match="Could not parse beam"):
        get_kernel(header=header)


def test_a_kernel_too_big_for_the_image_is_refused() -> None:
    """``pad_reflect`` is njit-ed without bounds checking, so a kernel wider than
    the image it pads reads off the end and returns quietly wrong maps. A small
    image with a big step downsamples into exactly that."""
    with pytest.raises(ValueError, match="does not fit"):
        robust_bane(
            image=np.zeros((64, 64), dtype=np.float32),
            header=_header(),
            fft_bane_options=FFTBANEOptions(step_size=16, box_size=32),
        )


def test_bane_fits_image_writes_maps_on_the_input_grid(tmp_path: Path) -> None:
    """The maps are stacked into cubes beside the image they came from, so they
    have to keep its shape, including the degenerate axes a linmos plane carries.
    """
    image = _sky(background=0.0, rms=1e-3)
    header = _header()
    # A linmos plane is (stokes, freq, ny, nx)
    fits_path = tmp_path / "field.image.fits"
    fits.writeto(fits_path, image[np.newaxis, np.newaxis], header, overwrite=True)

    bkg_path, rms_path = bane_fits_image(image=fits_path)

    assert bkg_path == tmp_path / "field.image_bkg.fits"
    assert rms_path == tmp_path / "field.image_rms.fits"

    for path in (bkg_path, rms_path):
        assert path.exists()
        assert fits.getdata(path).shape == (1, 1, NY, NX)

    assert np.nanmedian(fits.getdata(rms_path)) == pytest.approx(1e-3, rel=0.3)


def test_bane_fits_image_refuses_a_cube(tmp_path: Path) -> None:
    """Only single planes are ported, so a real cube is an error rather than a
    silently measured first channel."""
    fits_path = tmp_path / "cube.fits"
    fits.writeto(
        fits_path, np.zeros((4, 32, 32), dtype=np.float32), _header(), overwrite=True
    )

    with pytest.raises(ValueError, match="expected a single plane"):
        bane_fits_image(image=fits_path)


@pytest.mark.parametrize("beamless", ["missing", "zero"])
def test_bane_fits_image_blanks_a_plane_with_no_beam(
    tmp_path: Path, beamless: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A blank channel loses its beam keywords through linmos, or carries the
    zero beam that marks it as holding no PSF. Neither can size a kernel, and
    neither holds signal, so the maps come out blank rather than raising and
    taking the whole cube with them."""
    header = _header()
    for key in ("BMAJ", "BMIN", "BPA"):
        if beamless == "missing":
            del header[key]
        else:
            header[key] = 0.0

    fits_path = tmp_path / "field.image.fits"
    fits.writeto(fits_path, _sky()[np.newaxis, np.newaxis], header, overwrite=True)

    with caplog.at_level(logging.WARNING, logger="flint"):
        bkg_path, rms_path = bane_fits_image(image=fits_path)

    # A zero beam sizes a 1x1 all-NaN kernel rather than raising, so the maps
    # coming out blank does not on its own say the beam was checked
    assert "No usable beam" in caplog.text

    for path in (bkg_path, rms_path):
        assert fits.getdata(path).shape == (1, 1, NY, NX)
        assert np.all(np.isnan(fits.getdata(path)))


def test_robust_bane_without_a_beam_runs_on_given_sizes() -> None:
    """The beam only sizes the kernel, so sizes given outright need no beam"""
    header = _header()
    for key in ("BMAJ", "BMIN", "BPA"):
        del header[key]

    background, rms = robust_bane(
        image=_sky(background=0.0, rms=1e-3),
        header=header,
        fft_bane_options=FFTBANEOptions(step_size=10, box_size=10),
    )
    assert np.isfinite(background).all()
    assert np.nanmedian(rms) == pytest.approx(1e-3, rel=0.3)
