"""Tests for the FFT BANE port in ``flint.bane``"""

from __future__ import annotations

import logging
import tracemalloc
import warnings
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from scipy import ndimage

from flint.bane import (
    FFTBANEOptions,
    bane_fits_image,
    bilinear_upsample,
    block_stats,
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
    """Agrees with np.pad(mode='reflect')"""
    rng = np.random.default_rng(0)
    array = rng.normal(size=(17, 23)).astype(np.float32)

    for pad_y, pad_x in ((1, 1), (5, 3), (8, 11)):
        assert np.allclose(
            pad_reflect(array, (pad_y, pad_x)),
            np.pad(array, ((pad_y, pad_y), (pad_x, pad_x)), mode="reflect"),
        ), (pad_y, pad_x)


def test_fft_average_preserves_a_flat_image() -> None:
    """Smoothing a constant image returns that constant"""
    image = np.full((128, 128), 3.0, dtype=np.float32)

    for kernel in (gaussian_kernel(5), tophat_kernel(8)):
        assert np.allclose(fft_average(image, kernel), 3.0, atol=1e-4)


def test_bane_recovers_a_varying_background_and_rms() -> None:
    """Tracks a background and noise that both vary across the plane"""
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

    assert np.nanmedian(background) == pytest.approx(np.median(truth_bkg), rel=0.3)
    assert np.nanmedian(rms) == pytest.approx(np.median(truth_rms), rel=0.3)

    assert np.nanmedian(rms[:, -100:]) > 1.5 * np.nanmedian(rms[:, :100])
    assert np.nanmedian(background[-100:, :]) > np.nanmedian(background[:100, :])


def test_blanked_pixels_stay_blank() -> None:
    """NaN pixels come back NaN in both maps"""
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
    """A zero-filled footprint measures the same as a NaN-filled one"""
    inside = _footprint(radius=380)
    assert (~inside).mean() > 0.5, "the collapse needs over half the plane blank"

    sky = _sky(background=5e-4, rms=1e-3)
    zero_filled = np.where(inside, sky, 0.0).astype(np.float32)
    nan_filled = np.where(inside, sky, np.nan).astype(np.float32)

    zero_bkg, zero_rms = robust_bane(image=zero_filled, header=_header())
    nan_bkg, nan_rms = robust_bane(image=nan_filled, header=_header())

    assert np.nanmedian(zero_rms[inside]) == pytest.approx(1e-3, rel=0.3)
    assert np.nanmedian(zero_rms[inside]) == pytest.approx(
        np.nanmedian(nan_rms[inside]), rel=0.05
    )
    assert np.nanmedian(zero_bkg[inside]) == pytest.approx(
        np.nanmedian(nan_bkg[inside]), rel=0.05
    )

    assert np.isnan(zero_rms[~inside]).all()
    assert np.isnan(zero_bkg[~inside]).all()


def test_invalidate_zeros_can_be_turned_off() -> None:
    """With invalidate_zeros off, zeros are measured as data"""
    inside = _footprint(radius=380)
    zero_filled = np.where(inside, _sky(rms=1e-3), 0.0).astype(np.float32)

    _, rms = robust_bane(
        image=zero_filled,
        header=_header(),
        fft_bane_options=FFTBANEOptions(invalidate_zeros=False),
    )

    assert np.isfinite(rms).all()
    assert np.median(rms[~_footprint(radius=480)]) < 1e-4
    assert np.median(rms[_footprint(radius=300)]) == pytest.approx(1e-3, rel=0.3)


def test_the_rms_map_is_never_negative() -> None:
    """The RMS map has no negative values at a footprint edge"""
    inside = _footprint(radius=480)
    sky = _sky(rms=1e-3)

    for filled in (
        np.where(inside, sky, np.nan).astype(np.float32),
        np.where(inside, sky, 0.0).astype(np.float32),
    ):
        _, rms = robust_bane(image=filled, header=_header())
        assert not np.any(rms[np.isfinite(rms)] < 0.0)


def test_a_wholly_blank_plane_returns_blank_maps_quietly() -> None:
    """An all-blank plane gives blank maps without numpy warnings"""
    for plane in (
        np.full((NY, NX), np.nan, dtype=np.float32),
        np.zeros((NY, NX), dtype=np.float32),
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            background, rms = robust_bane(image=plane, header=_header())

        assert np.isnan(background).all()
        assert np.isnan(rms).all()


def test_the_seed_makes_a_rerun_reproducible() -> None:
    """The same seed gives the same maps, a different one does not"""
    image = _sky()
    # Low enough that the refilled pixels survive the downsampling
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
    """Unset sizes are 3 and 10 beams; negative sizes set the beam count"""
    kernel, step = get_kernel(header=_header())
    assert step == 3 * PIX_PER_BEAM
    assert kernel.max() == pytest.approx(1.0)

    _, step = get_kernel(header=_header(), step_size=-5)
    assert step == 5 * PIX_PER_BEAM

    kernel, step = get_kernel(header=_header(), step_size=7, box_size=4)
    assert step == 7
    assert kernel.shape == gaussian_kernel(4).shape


def test_get_kernel_needs_a_beam_it_can_read() -> None:
    """Sizing from a header with no beam raises"""
    header = _header()
    for key in ("BMAJ", "BMIN", "BPA"):
        del header[key]

    with pytest.raises(ValueError, match="Could not parse beam"):
        get_kernel(header=header)


def test_a_kernel_too_big_for_the_image_is_refused() -> None:
    """A kernel wider than the downsampled image raises"""
    with pytest.raises(ValueError, match="does not fit"):
        robust_bane(
            # Not zeros: those are blank now, and a wholly blank plane returns
            # blank maps before the kernel is ever pressed against the image
            image=np.ones((64, 64), dtype=np.float32),
            header=_header(),
            fft_bane_options=FFTBANEOptions(step_size=16, box_size=32),
        )


def test_bane_fits_image_writes_maps_on_the_input_grid(tmp_path: Path) -> None:
    """The maps keep the input's name, shape and degenerate axes"""
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
    """A cube raises rather than measuring its first channel"""
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
    """A missing or zero beam gives blank maps and a warning"""
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

    assert "No usable beam" in caplog.text

    for path in (bkg_path, rms_path):
        assert fits.getdata(path).shape == (1, 1, NY, NX)
        assert np.all(np.isnan(fits.getdata(path)))


def test_robust_bane_without_a_beam_runs_on_given_sizes() -> None:
    """Sizes given outright need no beam"""
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


def test_the_working_memory_stays_a_small_multiple_of_the_plane() -> None:
    """Peak memory stays a small multiple of the plane"""
    image = _sky()
    header = _header()

    # Compile first, as compilation allocates
    robust_bane(image=image, header=header)

    for options in (
        FFTBANEOptions(),
        FFTBANEOptions(step_size=8, box_size=12),
        # Makes a source of most of the plane
        FFTBANEOptions(clip_sigma=1.0),
    ):
        tracemalloc.start()
        robust_bane(image=image, header=header, fft_bane_options=options)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < 7 * image.nbytes, (
            f"{options} peaked at {peak / image.nbytes:.1f}x the plane"
        )


def test_a_wider_plane_is_measured_as_float32() -> None:
    """A float64 plane gives the same float32 maps"""
    image = _sky()
    background, rms = robust_bane(image=image, header=_header())
    wide_background, wide_rms = robust_bane(
        image=image.astype(np.float64), header=_header()
    )

    assert wide_background.dtype == background.dtype == np.float32
    assert wide_rms.dtype == rms.dtype == np.float32
    assert np.array_equal(wide_background, background, equal_nan=True)
    assert np.array_equal(wide_rms, rms, equal_nan=True)


def test_fft_average_puts_the_smoothed_pixel_over_the_pixel_it_smooths() -> None:
    """Smoothing does not shift the image"""
    image = np.zeros((64, 64), dtype=np.float32)
    image[32, 20] = 1.0

    for kernel in (gaussian_kernel(6), gaussian_kernel(9), tophat_kernel(8)):
        kernel = (kernel / kernel.max()).astype(np.float32)
        smoothed = fft_average(np.ascontiguousarray(image), kernel)

        # Not argmax, which a tophat's flat disc puts on its first row
        centre = ndimage.center_of_mass(smoothed)
        assert centre == pytest.approx((32.0, 20.0), abs=0.01), (
            f"a {kernel.shape} kernel moved the delta to {centre}"
        )

        # np.pad's "reflect" is scipy's "mirror"
        centred = ndimage.convolve(image, kernel / kernel.sum(), mode="mirror")
        assert np.allclose(smoothed, centred, atol=1e-6)


def test_the_noise_map_lines_up_with_the_noise_it_measures() -> None:
    """A patch of louder noise stays where it is in the RMS map"""
    shape = (512, 512)
    centre_y, centre_x = 300, 180
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float32)
    amplitude = 1.0 + 5.0 * np.exp(
        -0.5 * (((xx - centre_x) / 50) ** 2 + ((yy - centre_y) / 50) ** 2)
    )
    rng = np.random.default_rng(11)
    image = (rng.normal(0.0, 1e-3, shape) * amplitude).astype(np.float32)

    _, rms = robust_bane(
        image=image,
        header=_header(shape),
        fft_bane_options=FFTBANEOptions(step_size=10, box_size=6),
    )

    # Not argmax, which wanders by tens of pixels with the noise
    excess = np.clip(rms - np.nanmedian(rms), 0.0, None)
    peak_y, peak_x = ndimage.center_of_mass(excess)
    assert abs(peak_y - centre_y) < 10, f"noise peak {peak_y:.1f} rows from {centre_y}"
    assert abs(peak_x - centre_x) < 10, (
        f"noise peak {peak_x:.1f} columns from {centre_x}"
    )


def test_a_plane_measured_without_downsampling() -> None:
    """step_size=0 works and does not shift the RMS map"""
    shape = (512, 512)
    centre_y, centre_x = 300, 180
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float32)
    amplitude = 1.0 + 5.0 * np.exp(
        -0.5 * (((xx - centre_x) / 50) ** 2 + ((yy - centre_y) / 50) ** 2)
    )
    rng = np.random.default_rng(11)
    image = (rng.normal(0.0, 1e-3, shape) * amplitude).astype(np.float32)

    background, rms = robust_bane(
        image=image,
        header=_header(shape),
        fft_bane_options=FFTBANEOptions(step_size=0, box_size=12),
    )

    assert np.isfinite(background).all()
    assert np.isfinite(rms).all()
    excess = np.clip(rms - np.nanmedian(rms), 0.0, None)
    peak_y, peak_x = ndimage.center_of_mass(excess)
    assert abs(peak_y - centre_y) < 10, f"noise peak {peak_y:.1f} rows from {centre_y}"
    assert abs(peak_x - centre_x) < 10, (
        f"noise peak {peak_x:.1f} columns from {centre_x}"
    )


def test_a_region_of_loud_artefacts_is_measured_as_noise() -> None:
    """A loud artefact region is measured as loud, not clipped away"""
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:NY, 0:NX]
    truth = np.full((NY, NX), 1e-3, dtype=np.float32)
    truth[(yy < 450) & (xx < 450)] = 0.1
    image = (rng.normal(0, 1, (NY, NX)) * truth).astype(np.float32)

    _, rms = robust_bane(image=image, header=_header())

    assert np.nanmedian(rms[50:400, 50:400]) == pytest.approx(0.1, rel=0.3)
    assert np.nanmedian(rms[600:, 600:]) == pytest.approx(1e-3, rel=0.3)


def test_a_source_larger_than_a_block_is_still_clipped() -> None:
    """A source larger than one seed tile does not raise the RMS around it"""
    image = _sky(rms=1e-3)
    image[600:640, 850:890] += 0.05

    _, rms = robust_bane(image=image, header=_header())

    assert np.nanmedian(rms[590:650, 840:900]) == pytest.approx(1e-3, rel=0.2)


@pytest.mark.filterwarnings("ignore:All-NaN slice:RuntimeWarning")
def test_block_stats_matches_numpy() -> None:
    """Agrees with numpy's nanmedian, with sparse tiles left NaN"""
    rng = np.random.default_rng(0)
    image = rng.normal(size=(40, 60)).astype(np.float32)
    nan_mask = rng.random(image.shape) < 0.2
    nan_mask[:10, :10] = True
    nan_mask[10:20, 10:20] = rng.random((10, 10)) < 0.8

    median, mad = block_stats(image, nan_mask, 10)

    blocks = np.where(nan_mask, np.nan, image).reshape(4, 10, 6, 10).swapaxes(1, 2)
    blocks = blocks.reshape(4, 6, 100)
    expected = np.nanmedian(blocks, axis=-1)
    expected_mad = np.nanmedian(np.abs(blocks - expected[..., None]), axis=-1)
    too_blank = np.isfinite(blocks).sum(axis=-1) < 25
    expected[too_blank] = expected_mad[too_blank] = np.nan

    assert too_blank[0, 0]
    assert np.allclose(median, expected, equal_nan=True)
    assert np.allclose(mad, expected_mad, equal_nan=True)


def test_the_step_back_up_matches_scipy() -> None:
    """Agrees with scipy's linear affine_transform"""
    grid = np.random.default_rng(0).normal(size=(17, 23)).astype(np.float32)
    shape = (500, 700)

    for start, step in ((30.0, 30.0), (14.5, 30.0)):
        expected = ndimage.affine_transform(
            grid,
            matrix=np.full(2, 1 / step),
            offset=np.full(2, -start / step),
            output_shape=shape,
            order=1,
            mode="nearest",
        )
        assert np.allclose(
            bilinear_upsample(grid, start, step, shape), expected, atol=1e-5
        )
