"""BANE, the background and noise estimator, computed with FFTs.

Ported from ``AegeanTools.BANE_fft`` (AlecThomson/Aegean, ``dask`` branch). Here
``step`` is a downsampling factor and ``box`` a kernel size, and the box average
is a convolution. Only the 2D single-plane routines: callers run this per channel.

``rocket_fft`` is imported for its side effect of teaching numba ``numpy.fft``,
without which every ``njit`` here fails to compile.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import astropy.units as u
import numba as nb
import numpy as np
import rocket_fft  # noqa: F401
from astropy.io import fits
from astropy.stats import mad_std
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from numpy import fft
from numpy.typing import NDArray
from radio_beam import Beam
from radio_beam.beam import NoBeamException
from scipy import ndimage

from flint.logging import logger
from flint.naming import create_aegean_names
from flint.options import FFTBANEOptions


class BANEMaps(NamedTuple):
    """The pair of maps ``bane_fits_image`` writes"""

    bkg_image: Path
    """Background map"""
    rms_image: Path
    """RMS noise map"""


@nb.njit(fastmath=True, cache=True)
def _ft_kernel(kernel: NDArray[np.float32], shape: tuple) -> NDArray[np.float32]:
    """FFT of `kernel`, zero-padded out to `shape`"""
    return fft.rfft2(kernel, s=shape)


@nb.njit(
    nb.float32[:, :](
        nb.float32[:, :],
        nb.types.UniTuple(nb.int64, 2),
    ),
    fastmath=True,
    cache=True,
)
def pad_reflect(
    array: NDArray[np.float32],
    pad_width: tuple[int, int],
) -> NDArray[np.float32]:
    """``np.pad(array, pad_width, mode="reflect")``, in a form numba can compile"""
    nx, ny = array.shape
    px, py = pad_width

    padded = np.empty((nx + 2 * px, ny + 2 * py), dtype=array.dtype)
    padded[px : px + nx, py : py + ny] = array

    for i in range(px):
        padded[px - 1 - i, py : py + ny] = array[i + 1, :]
        padded[nx + px + i, py : py + ny] = array[nx - 2 - i, :]

    for j in range(py):
        padded[:, py - 1 - j] = padded[:, py + j + 1]
        padded[:, ny + py + j] = padded[:, ny + py - 2 - j]

    return padded


@nb.njit(
    nb.float32[:, :](nb.float32[:, :], nb.float32[:, :]),
    fastmath=True,
    cache=True,
)
def fft_average(
    image: NDArray[np.float32], kernel: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Convolve `image` by `kernel`, normalised so a flat image is unchanged.

    Reflect-padded by the kernel size so the FFT's periodic wrap does not fold
    the far edge of the image back onto the near one.
    """
    pad_x, pad_y = kernel.shape
    image_padded = pad_reflect(array=image, pad_width=(pad_x, pad_y))

    image_fft = fft.rfft2(image_padded)
    kernel_fft = _ft_kernel(kernel, shape=image_padded.shape)
    smooth = fft.irfft2(image_fft * kernel_fft, s=image_padded.shape) / kernel.sum()

    return smooth[pad_x:-pad_x, pad_y:-pad_y]


@nb.njit(
    nb.types.UniTuple(nb.float32[:, :], 2)(
        nb.float32[:, :], nb.float32[:, :], nb.float32[:, :]
    ),
    fastmath=True,
    cache=True,
)
def bane_fft(
    image: NDArray[np.float32],
    kernel: NDArray[np.float32],
    valid: NDArray[np.float32],
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Background and RMS of `image`, as `kernel`-weighted local averages.

    `image` must be zero wherever `valid` is zero. Dividing by the smoothed
    validity mask stops a blank counting as a measured zero, which would
    otherwise drag the background down within a kernel width of every blank.
    """
    weight = fft_average(valid, kernel)
    weight = np.where(weight > 0, weight, np.nan).astype(np.float32)

    mean = (fft_average(image, kernel) / weight).astype(np.float32)
    # Blanked pixels hold zero, not the mean, so their residual has to be
    # zeroed too or it would enter the sum as the full mean squared. Selected
    # rather than multiplied by `valid`: `mean` is NaN where nothing was valid,
    # and NaN * 0 is NaN, which the FFT would then spread over the whole plane
    resid = np.where(valid > 0, (image - mean) ** 2, np.float32(0.0)).astype(np.float32)
    rms = np.sqrt(fft_average(resid, kernel) / weight).astype(np.float32)

    return mean, rms


def tophat_kernel(diameter: int) -> NDArray[np.float32]:
    """Circular tophat kernel of `diameter` pixels"""
    radius = diameter // 2
    kernel = np.zeros((radius * 2 + 1, radius * 2 + 1), dtype=np.float32)
    xx = np.arange(-radius, radius + 1)
    X, Y = np.meshgrid(xx, xx)
    kernel[radius**2 >= X**2 + Y**2] = 1
    return kernel


def gaussian_kernel(fwhm: int) -> NDArray[np.float32]:
    """Gaussian kernel of `fwhm` pixels"""
    xx = np.arange(-fwhm, fwhm + 1)
    X, Y = np.meshgrid(xx, xx)
    kernel = np.exp(-4 * np.log(2) * (X**2 + Y**2) / fwhm**2)
    return kernel.astype(np.float32)


def get_kernel(
    header: fits.Header | dict[str, Any],
    step_size: int | None = None,
    box_size: int | None = None,
    kernel_func: Callable[[int], NDArray[np.float32]] = gaussian_kernel,
) -> tuple[NDArray[np.float32], int]:
    """The convolution kernel and downsampling factor, in pixels.

    ``step_size`` is the downsampling factor and ``box_size`` the kernel size.
    Either being None or negative sizes it from the restoring beam instead, at
    3 and 10 beams respectively, or at ``abs(value)`` beams when negative.

    Args:
        header (fits.Header | dict[str, Any]): Header of the image, for the beam and pixel scale
        step_size (int | None, optional): Downsampling factor in pixels. Defaults to 3 beams.
        box_size (int | None, optional): Kernel size in pixels. Defaults to 10 beams.
        kernel_func (Callable, optional): Kernel shape. Defaults to ``gaussian_kernel``.

    Returns:
        tuple[NDArray[np.float32], int]: The kernel, peak-normalised, and the step size in pixels
    """
    if step_size is None or step_size < 0 or box_size is None or box_size < 0:
        try:
            beam = Beam.from_fits_header(header)
            scales = proj_plane_pixel_scales(WCS(header)) * u.deg / u.pixel
            pix_per_beam = beam.minor / scales.min()
        except (ValueError, NoBeamException) as error:
            # radio_beam raises NoBeamException, which is not a ValueError, so
            # catching ValueError alone lets a header with no beam through
            msg = "Could not parse beam from header - try specifying step size"
            raise ValueError(msg) from error
        logger.info(f"{beam!r}, {pix_per_beam:0.1f} pixels per beam")

    if step_size is None or step_size < 0:
        nbeam_step = 3 if step_size is None else abs(step_size)
        step_size_pix = int(np.ceil((nbeam_step * pix_per_beam).to(u.pix).value))
    else:
        step_size_pix = step_size

    if box_size is None or box_size < 0:
        nbeam_box = 10 if box_size is None else abs(box_size)
        scaler = step_size_pix if step_size_pix > 0 else 1
        box_size_pix = abs(int(np.ceil(pix_per_beam.value * nbeam_box / scaler)))
    else:
        box_size_pix = box_size

    logger.info(f"BANE {step_size_pix=} {box_size_pix=} (box is post-downsampling)")

    kernel = kernel_func(box_size_pix)
    kernel /= kernel.max()

    return kernel, step_size_pix


def _downsample_slices(
    shape: tuple[int, int], step_size_pix: int
) -> tuple[slice, slice]:
    """Slices taking every `step_size_pix` pixel, trimmed to an even count.

    The sampled region runs from `step_size_pix` to `length - step_size_pix`
    while the zoom back up stretches it over the whole image. That is the likely
    cause of the offset upstream records as a TODO; sampling half a cell in
    instead measured worse, so this keeps upstream's grid.
    """
    slices = []
    for length in (shape[0], shape[1]):
        stop = length - step_size_pix
        while (stop // step_size_pix) % 2 != 0:
            stop -= 1
        slices.append(slice(step_size_pix, stop, step_size_pix))
    return slices[0], slices[1]


def _bane_round(
    filled: NDArray[np.float32],
    valid: NDArray[np.float32],
    nan_mask: NDArray[np.bool_],
    background: NDArray[np.float32],
    rms: NDArray[np.float32],
    kernel: NDArray[np.float32],
    step_size_pix: int,
    clip_sigma: float,
    rng: np.random.Generator,
    round_number: int,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """One clip-refill-smooth pass, clipping against the given background and RMS.

    The refill carries the local background: zero-mean noise would drag the
    background down wherever a source was removed.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        source_mask = (np.abs(filled - background) / rms > clip_sigma) & ~nan_mask
    logger.info(
        f"BANE round {round_number}: refilling {source_mask.sum()} "
        f"({source_mask.sum() / filled.size * 100:0.1f}%) source pixels with noise"
    )

    clipped = filled.copy()
    clipped[source_mask] = (
        background[source_mask]
        + rng.normal(loc=0, scale=1, size=source_mask.sum()) * rms[source_mask]
    )
    # Blanked pixels must stay at zero: bane_fft normalises by the smoothed
    # validity mask and assumes they contribute nothing
    clipped[nan_mask] = 0.0

    zoom: tuple[float, float] | None = None
    round_valid = valid
    if step_size_pix > 0:
        y_slice, x_slice = _downsample_slices(clipped.shape, step_size_pix)
        zoom = (
            clipped.shape[0] / clipped[y_slice, x_slice].shape[0],
            clipped.shape[1] / clipped[y_slice, x_slice].shape[1],
        )
        clipped, round_valid = clipped[y_slice, x_slice], valid[y_slice, x_slice]

    # pad_reflect is njit-ed without bounds checking, so a kernel wider than the
    # image reads off the end and returns quietly wrong maps rather than raising
    if any(pad >= length for pad, length in zip(kernel.shape, clipped.shape)):
        msg = (
            f"A {kernel.shape} kernel does not fit the {clipped.shape} image it "
            "smooths. Lower step_size so less is downsampled away, or box_size "
            "for a smaller kernel."
        )
        raise ValueError(msg)

    background, rms = bane_fft(
        np.ascontiguousarray(clipped), kernel, np.ascontiguousarray(round_valid)
    )
    background = np.nan_to_num(background, nan=0.0)
    rms = np.nan_to_num(rms, nan=0.0)

    if zoom is not None:
        background = ndimage.zoom(
            background, zoom, order=3, grid_mode=True, mode="reflect"
        )
        rms = ndimage.zoom(rms, zoom, order=3, grid_mode=True, mode="reflect")

    return background, rms


def robust_bane(
    image: NDArray[np.float32],
    header: fits.Header | dict[str, Any],
    fft_bane_options: FFTBANEOptions | None = None,
    kernel_func: Callable[[int], NDArray[np.float32]] = gaussian_kernel,
    rms_estimator: Callable[[NDArray[np.float32]], float] = mad_std,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Background and RMS maps of a single image plane.

    Two passes: the first clips sources against one background and noise for the
    plane, the second against the maps the first produced.

    Args:
        image (NDArray[np.float32]): The image plane to measure
        header (fits.Header | dict[str, Any]): Its header, for the beam and pixel scale
        fft_bane_options (FFTBANEOptions | None, optional): Step, box, clip and seed. Defaults to ``FFTBANEOptions()``.
        kernel_func (Callable, optional): Kernel shape. Defaults to ``gaussian_kernel``.
        rms_estimator (Callable, optional): First-pass RMS estimator. Defaults to ``mad_std``.

    Returns:
        tuple[NDArray[np.float32], NDArray[np.float32]]: Background and RMS, shaped like `image`
    """
    fft_bane_options = fft_bane_options or FFTBANEOptions()
    kernel, step_size_pix = get_kernel(
        header=header,
        step_size=fft_bane_options.step_size,
        box_size=fft_bane_options.box_size,
        kernel_func=kernel_func,
    )

    nan_mask = ~np.isfinite(image)
    valid = (~nan_mask).astype(np.float32)
    filled = np.where(nan_mask, 0.0, image).astype(np.float32)
    finite = image[~nan_mask].ravel()

    # The median matters: testing |image| rather than |image - background| makes
    # every pixel a source as soon as the plane carries a DC offset
    background = np.full_like(filled, float(np.median(finite)))
    rms = np.full_like(filled, float(rms_estimator(finite)))

    # A mosaic's noise rises with the primary beam, so one threshold clips real
    # noise at the edge while missing faint sources in the middle
    rng = np.random.default_rng(fft_bane_options.seed)
    for round_number in (1, 2):
        background, rms = _bane_round(
            filled=filled,
            valid=valid,
            nan_mask=nan_mask,
            background=background,
            rms=rms,
            kernel=kernel,
            step_size_pix=step_size_pix,
            clip_sigma=fft_bane_options.clip_sigma,
            rng=rng,
            round_number=round_number,
        )

    background[nan_mask] = np.nan
    rms[nan_mask] = np.nan

    return background, rms


def bane_fits_image(
    image: Path,
    fft_bane_options: FFTBANEOptions | None = None,
) -> BANEMaps:
    """Write the background and RMS maps of a single-plane FITS image.

    Named ``_bkg.fits`` and ``_rms.fits`` beside the input, as the aegean BANE
    names them, so the two are interchangeable downstream.

    Args:
        image (Path): Single-plane FITS image to measure
        fft_bane_options (FFTBANEOptions | None, optional): Step, box, clip and seed. Defaults to ``FFTBANEOptions()``.

    Returns:
        BANEMaps: The background and RMS maps written
    """
    logger.info(f"Running FFT BANE on {image}")
    with fits.open(image, memmap=True, mode="denywrite") as hdul:
        header = hdul[0].header
        # A linmos plane carries degenerate Stokes/frequency axes; the maps are
        # written back with them so they stack like the image they came from
        original_shape = hdul[0].data.shape
        data = np.squeeze(hdul[0].data).astype(np.float32)

    if data.ndim != 2:
        msg = f"{image} has a {data.ndim}D image once squeezed, expected a single plane"
        raise ValueError(msg)

    background, rms = robust_bane(
        image=data, header=header, fft_bane_options=fft_bane_options
    )

    names = create_aegean_names(base_output=str(image.parent / image.stem))
    for data_out, path in ((background, names.bkg_image), (rms, names.rms_image)):
        # The input header, so the maps stay on its pixel grid
        fits.writeto(path, data_out.reshape(original_shape), header, overwrite=True)
        logger.info(f"Wrote {path}")

    return BANEMaps(bkg_image=names.bkg_image, rms_image=names.rms_image)
