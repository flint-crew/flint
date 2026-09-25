"""BANE, the background and noise estimator, computed with FFTs.

Ported from ``AegeanTools.BANE_fft``, single planes only. ``step`` is a
downsampling factor and ``box`` a kernel size.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import astropy.units as u
import numba as nb
import numpy as np
import rocket_fft  # noqa: F401  (teaches numba numpy.fft)
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from numpy import fft
from numpy.typing import NDArray
from radio_beam import Beam
from radio_beam.beam import NoBeamException
from scipy import ndimage

from flint.convol import header_beam_is_usable
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
    """Convolve `image` by `kernel`, normalised so a flat image is unchanged"""
    pad_x, pad_y = kernel.shape
    image_padded = pad_reflect(array=image, pad_width=(pad_x, pad_y))

    image_fft = fft.rfft2(image_padded)
    kernel_fft = _ft_kernel(kernel, shape=image_padded.shape)
    smooth = fft.irfft2(image_fft * kernel_fft, s=image_padded.shape) / kernel.sum()

    # The kernel is padded from index zero, so the result is shifted half a kernel
    nx, ny = image.shape
    start_x, start_y = pad_x + pad_x // 2, pad_y + pad_y // 2
    return smooth[start_x : start_x + nx, start_y : start_y + ny]


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
    """Background and RMS of `image`, which must be zero wherever `valid` is"""
    weight = fft_average(valid, kernel)
    weight = np.where(weight > 0, weight, np.nan).astype(np.float32)

    mean = (fft_average(image, kernel) / weight).astype(np.float32)
    # Selected rather than multiplied by `valid`, as NaN * 0 is NaN
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

    A size that is None or negative is taken from the beam: 3 beams for the
    step and 10 for the box, or ``abs(value)`` beams when negative.

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
    """Slices taking every `step_size_pix` pixel from `step_size_pix` on, an even count"""
    slices = []
    for length in (shape[0], shape[1]):
        stop = length - step_size_pix
        while (stop // step_size_pix) % 2 != 0:
            stop -= 1
        slices.append(slice(step_size_pix, stop, step_size_pix))
    return slices[0], slices[1]


@nb.njit(parallel=True, cache=True)
def bilinear_upsample(
    grid: NDArray[np.float32], start: float, step: float, shape: tuple[int, int]
) -> NDArray[np.float32]:
    """Interpolate `grid`, sampled at pixels `start + k * step`, onto `shape`"""
    axes = []
    for axis in range(2):
        n = grid.shape[axis]
        position = (np.arange(shape[axis]) - start) / step
        position = np.minimum(np.maximum(position, 0.0), n - 1)
        lower = np.minimum(position.astype(np.int64), max(n - 2, 0))
        axes.append((lower, np.minimum(lower + 1, n - 1), position - lower))
    (y0, y1, wy), (x0, x1, wx) = axes[0], axes[1]

    out = np.empty(shape, dtype=np.float32)
    for i in nb.prange(shape[0]):
        a, b, fy = y0[i], y1[i], wy[i]
        for j in range(shape[1]):
            c, d, fx = x0[j], x1[j], wx[j]
            top = grid[a, c] + fx * (grid[a, d] - grid[a, c])
            bottom = grid[b, c] + fx * (grid[b, d] - grid[b, c])
            out[i, j] = top + fy * (bottom - top)
    return out


@nb.njit(parallel=True, cache=True)
def block_stats(
    image: NDArray[np.float32],
    nan_mask: NDArray[np.bool_],
    block: int,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Median and MAD of each `block`-square tile, NaN if under a quarter valid"""
    n_y, n_x = image.shape[0] // block, image.shape[1] // block
    median = np.full((n_y, n_x), np.nan, dtype=np.float32)
    mad = np.full((n_y, n_x), np.nan, dtype=np.float32)
    for row in nb.prange(n_y):
        values = np.empty(block * block, dtype=np.float32)
        for col in range(n_x):
            count = 0
            for y in range(row * block, (row + 1) * block):
                for x in range(col * block, (col + 1) * block):
                    if not nan_mask[y, x]:
                        values[count] = image[y, x]
                        count += 1
            if count < block * block // 4:
                continue
            centre = np.median(values[:count])
            median[row, col] = centre
            mad[row, col] = np.median(np.abs(values[:count] - centre))
    return median, mad


def local_seed(
    image: NDArray[np.float32],
    nan_mask: NDArray[np.bool_],
    block: int,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Background and RMS maps from the median and MAD of `block`-pixel tiles.

    What the first round clips against. A single value for the whole plane
    would clip away every loud artefact region as a source.
    """
    block = min(block, *image.shape)
    median, mad = block_stats(image, nan_mask, block)

    unmeasured = ~np.isfinite(mad)
    if unmeasured.all():
        valid = image[~nan_mask]
        median[:] = np.median(valid)
        mad[:] = np.median(np.abs(valid - median[0, 0]))
    elif unmeasured.any():
        _, nearest = ndimage.distance_transform_edt(unmeasured, return_indices=True)
        median, mad = median[tuple(nearest)], mad[tuple(nearest)]

    # Filtered so a source larger than one tile is still clipped
    background, rms = (
        bilinear_upsample(
            ndimage.median_filter(grid, size=3, mode="nearest"),
            (block - 1) / 2,
            block,
            image.shape,
        )
        for grid in (median, 1.4826 * mad)
    )
    return background, rms


def bane_round(
    image: NDArray[np.float32],
    nan_mask: NDArray[np.bool_],
    background: NDArray[np.float32],
    rms: NDArray[np.float32],
    kernel: NDArray[np.float32],
    step_size_pix: int,
    clip_sigma: float,
    rng: np.random.Generator,
    round_number: int,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Clip sources against `background` and `rms`, refill them with noise, smooth"""
    with np.errstate(invalid="ignore", divide="ignore"):
        # In place, to hold one full-size temporary rather than three
        deviation = image - background
        np.abs(deviation, out=deviation)
        deviation /= rms
        source_mask = deviation > clip_sigma
    del deviation
    source_mask[nan_mask] = False

    n_source = int(source_mask.sum())
    logger.info(
        f"BANE round {round_number}: refilling {n_source} "
        f"({n_source / image.size * 100:0.1f}%) source pixels with noise"
    )

    clipped = np.where(nan_mask, np.float32(0.0), image)
    clipped[source_mask] = (
        background[source_mask]
        + rng.normal(loc=0, scale=1, size=n_source) * rms[source_mask]
    )

    if step_size_pix > 0:
        y_slice, x_slice = _downsample_slices(image.shape, step_size_pix)
        downsampled = np.ascontiguousarray(clipped[y_slice, x_slice])
        round_valid = (~nan_mask[y_slice, x_slice]).astype(np.float32)
    else:
        downsampled = np.ascontiguousarray(clipped)
        round_valid = (~nan_mask).astype(np.float32)

    # pad_reflect has no bounds checks, so would read off the end instead
    if any(pad >= length for pad, length in zip(kernel.shape, downsampled.shape)):
        msg = (
            f"A {kernel.shape} kernel does not fit the {downsampled.shape} image it "
            "smooths. Lower step_size so less is downsampled away, or box_size "
            "for a smaller kernel."
        )
        raise ValueError(msg)

    del clipped, source_mask

    smooth_background, smooth_rms = bane_fft(downsampled, kernel, round_valid)
    np.nan_to_num(smooth_background, nan=0.0, copy=False)
    np.nan_to_num(smooth_rms, nan=0.0, copy=False)

    if step_size_pix > 0:
        smooth_background, smooth_rms = (
            bilinear_upsample(grid, step_size_pix, step_size_pix, image.shape)
            for grid in (smooth_background, smooth_rms)
        )

    return smooth_background, smooth_rms


def _needs_a_beam(fft_bane_options: FFTBANEOptions) -> bool:
    """Whether ``get_kernel`` will read the beam, rather than take both sizes as given"""
    step_size, box_size = fft_bane_options.step_size, fft_bane_options.box_size
    return step_size is None or step_size < 0 or box_size is None or box_size < 0


def robust_bane(
    image: NDArray[np.float32],
    header: fits.Header | dict[str, Any],
    fft_bane_options: FFTBANEOptions | None = None,
    kernel_func: Callable[[int], NDArray[np.float32]] = gaussian_kernel,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Background and RMS maps of a single image plane.

    Two rounds: the first clips against ``local_seed``, the second against the
    first's maps. Non-finite pixels are blank, as are zeros unless
    ``invalidate_zeros`` is unset. A plane that is all blank, or has no usable
    beam when one is needed, gets blank maps.

    Peaks at roughly five times the plane in memory.

    Args:
        image (NDArray[np.float32]): The image plane to measure
        header (fits.Header | dict[str, Any]): Its header, for the beam and pixel scale
        fft_bane_options (FFTBANEOptions | None, optional): Step, box, clip, seed and zero blanking. Defaults to ``FFTBANEOptions()``.
        kernel_func (Callable, optional): Kernel shape. Defaults to ``gaussian_kernel``.

    Returns:
        tuple[NDArray[np.float32], NDArray[np.float32]]: Background and RMS, shaped like `image`
    """
    fft_bane_options = fft_bane_options or FFTBANEOptions()

    if _needs_a_beam(fft_bane_options) and not header_beam_is_usable(header=header):
        logger.warning("No usable beam to size the BANE kernel, returning blank maps")
        blank = np.full_like(image, np.nan, dtype=np.float32)
        return blank, blank.copy()

    kernel, step_size_pix = get_kernel(
        header=header,
        step_size=fft_bane_options.step_size,
        box_size=fft_bane_options.box_size,
        kernel_func=kernel_func,
    )

    image = np.asarray(image, dtype=np.float32)

    nan_mask = ~np.isfinite(image)
    if fft_bane_options.invalidate_zeros:
        # linmos fills beyond its primary beam cutoff with zeros, not NaNs
        nan_mask |= image == 0.0

    if nan_mask.all():
        logger.warning("Every pixel of the plane is blank, returning blank maps")
        blank = np.full_like(image, np.nan, dtype=np.float32)
        return blank, blank.copy()

    background, rms = local_seed(
        image=image,
        nan_mask=nan_mask,
        block=step_size_pix if step_size_pix > 0 else kernel.shape[0],
    )
    rng = np.random.default_rng(fft_bane_options.seed)
    for round_number in (1, 2):
        background, rms = bane_round(
            image=image,
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
    """Write ``_bkg.fits`` and ``_rms.fits`` maps beside a single-plane FITS image.

    Args:
        image (Path): Single-plane FITS image to measure
        fft_bane_options (FFTBANEOptions | None, optional): Step, box, clip and seed. Defaults to ``FFTBANEOptions()``.

    Returns:
        BANEMaps: The background and RMS maps written
    """
    logger.info(f"Running FFT BANE on {image}")
    with fits.open(image, memmap=True, mode="denywrite") as hdul:
        header = hdul[0].header
        # Kept so the maps carry the same degenerate axes and stack alike
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
        fits.writeto(path, data_out.reshape(original_shape), header, overwrite=True)
        logger.info(f"Wrote {path}")

    return BANEMaps(bkg_image=names.bkg_image, rms_image=names.rms_image)
