"""Tests for the FFT BANE port in ``flint.bane``"""

from __future__ import annotations

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
