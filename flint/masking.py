"""Utility functions to make image based masks from images, with the initial
thought being towards FITS images.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Collection, NamedTuple

import astropy.units as u
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from numpy.typing import NDArray
from radio_beam import Beam
from reproject import reproject_interp
from scipy.ndimage import (
    binary_dilation as scipy_binary_dilation,  # Rename to distinguish from skimage
)
from scipy.ndimage import (
    binary_erosion as scipy_binary_erosion,  # Rename to distinguish from skimage
)
from scipy.ndimage import binary_fill_holes, label, minimum_filter
from scipy.signal import fftconvolve

from flint.logging import logger
from flint.naming import FITSMaskNames, create_fits_mask_names
from flint.options import BaseOptions, add_options_to_parser, create_options_from_parser
from flint.utils import get_pixels_per_beam

# TODO: Need to remove a fair amount of old approaches, and deprecate some of the toy functions


class MaskingOptions(BaseOptions):
    """Contains options for the creation of clean masks from some subject
    image. Clipping levels specified are in units of RMS (or sigma). They
    are NOT in absolute units.
    """

    base_snr_clip: float = 4
    """A base clipping level to be used should other options not be activated"""
    flood_fill: bool = False
    """Whether to attempt to flood fill when constructing a mask. This should be `True` for ``grow_low_snr_islands`` and ``suppress_artefacts`` to have an effect. """
    flood_fill_positive_seed_clip: float = 4.5
    """The clipping level to seed islands that will be grown to lower signal metric"""
    flood_fill_positive_flood_clip: float = 1.5
    """Clipping level used to grow seeded islands down to"""
    flood_fill_use_mac: bool = False
    """If True, the clipping levels are used as the `increase_factor` when using a minimum absolute clip"""
    flood_fill_use_mac_box_size: int = 75
    """The size of the mac box size should mac be used"""
    flood_fill_use_mac_adaptive_step_factor: float = 2.0
    """Stepping size used to increase box by should adaptive detect poor boxcar statistics"""
    flood_fill_use_mac_adaptive_skew_delta: float = 0.2
    """A box is consider too small for a pixel if the fractional proportion of positive pixels is larger than the deviation away of (0.5 + frac). This threshold is therefore 0 to 0.5"""
    flood_fill_use_mac_adaptive_max_depth: int | None = None
    """Determines the number of adaptive boxcar scales to use when constructing seed mask. If None no adaptive boxcar sizes"""
    grow_low_snr_island: bool = False
    """Whether to attempt to grow a mask to capture islands of low SNR (e.g. diffuse emission)"""
    grow_low_snr_island_clip: float = 1.75
    """The minimum significance levels of pixels to be to seed low SNR islands for consideration"""
    grow_low_snr_island_size: int = 768
    """The number of pixels an island has to be for it to be accepted"""
    beam_shape_erode: bool = False
    """Erode the mask using the shape of the restoring beam"""
    beam_shape_erode_minimum_response: float = 0.6
    """The minimum response of the beam that is used to form the erode structure shape"""
    beam_shape_erode_scales: tuple[int, ...] | None = None
    """The multi-scale convolution sizes, in pixels, to perform a binary erosion with. Output scales are encoded as a bitmapped value (e.g. n'th scale is n'th bit)"""
    convolve_first: bool = False
    """Attempt to construct mask across scales by first convolving the input image by a scale kernel, then run the island construction stage"""


def consider_beam_mask_round(
    current_round: int,
    mask_rounds: str | Collection[int] | int,
    allow_beam_masks: bool = True,
) -> bool:
    """Evaluate whether a self-calibration round should have a beam clean mask
    constructed. Rules are:

    - if `mask_rounds` is a string and is "all", all rounds will have a beam mask
    - if 'mask_rounds' is a single integer, so long as `current_round` is larger it will have a beam mask
    - if `mask_rounds` is iterable and contains `current_round` it will have a beam mask
    - if `allow_beam_masks` is False a False is returned. Otherwise options above are considered.

    Args:
        current_round (int): The current self-calibration round that is being performed
        mask_rounds (Union[str, Collection[int], int]): The rules to consider whether a beam mask is needed
        allow_beam_masks (bool, optional): A global allow / deny. This should be `True` for other rules to be considered. Defaults to True.

    Returns:
        bool: Whether per beam mask should be performed
    """
    logger.info(f"Considering {current_round=} {mask_rounds=} {allow_beam_masks=}")

    # The return below was getting silly to meantally parse
    if not allow_beam_masks:
        return False

    return mask_rounds is not None and (
        (isinstance(mask_rounds, str) and mask_rounds.lower() == "all")
        or (isinstance(mask_rounds, int) and current_round >= mask_rounds)
        or ((isinstance(mask_rounds, (list, tuple))) and current_round in mask_rounds)
    )  # type: ignore


def create_beam_mask_kernel(
    fits_header: fits.Header,
    kernel_size: int | tuple[int, int] = 100,
    minimum_response: float = 0.6,
    pixel_scale: int | None = None,
    auto_resize: bool = True,
) -> np.ndarray:
    """Make a mask using the shape of a beam in a FITS Header object. The
    beam properties in the ehader are used to generate the two-dimensional
    Gaussian main lobe, from which a cut is made based on the minimum
    power.

    Args:
        fits_header (fits.Header): The FITS header to create beam from
        kernel_size (int | tuple[int, int], optional): Size of the output kernel in pixels. Will be a square. Defaults to 100.
        minimum_response (float, optional): Minimum response of the beam shape for the mask to be constructed from. Defaults to 0.6.
        pixel_scale (int | None, optional): Convolve the restoring beam by this scale. The input scale is in pixel units. Defaults to None.
        auto_resize (bool, optional): If True and the kernel is evaluated to all 1's, enlarge the ``kernel_size`` by a factor of 2. Defaults to True.

    Raises:
        KeyError: Raised if CDELT1 and CDELT2 missing

    Returns:
        np.ndarray: Boolean mask of the kernel shape
    """
    assert 0.0 < minimum_response < 1.0, (
        f"{minimum_response=}, should be between 0 to 1 (exclusive)"
    )

    POSITION_KEYS = ("CDELT1", "CDELT2")
    if not all([key in fits_header for key in POSITION_KEYS]):
        raise KeyError(f"{POSITION_KEYS=}  all need to be present")

    beam = Beam.from_fits_header(fits_header)
    assert isinstance(beam, Beam)

    cdelt1, cdelt2 = np.abs(fits_header["CDELT1"]), np.abs(fits_header["CDELT2"])  # type: ignore
    assert np.isclose(cdelt1, cdelt2), (
        f"Pixel scales {cdelt1=} {cdelt2=}, but must be equal"
    )

    pixel_unit = u.Unit("deg")

    if pixel_scale is not None:
        assert isinstance(pixel_scale, int), (
            f"{pixel_scale=} and is of type {type(pixel_scale)}, but should be int"
        )
        logger.debug(f"Convoling {beam=} with {pixel_scale=}")
        arcsecond_kernel = int(pixel_scale) * cdelt1 * pixel_unit
        scale_beam = Beam(arcsecond_kernel, arcsecond_kernel, 0 * u.deg)

        logger.debug(f"Convolving with {scale_beam=}")
        beam = beam.convolve(scale_beam)

    if isinstance(kernel_size, int):
        kernel_size = (kernel_size, kernel_size)

    # Trust no one, mate
    assert isinstance(kernel_size, tuple) and len(kernel_size) == 2, (
        f"{kernel_size=}, but should be a tuple[int, int] by now"
    )

    for iteration in range(10):
        y_kernel_size, x_kernel_size = kernel_size

        # NOTE: This could be slow for large sizes.
        # TODO: Consider evaluating the inner, padding the outer?
        k = beam.as_kernel(
            pixscale=cdelt1 * pixel_unit, x_size=x_kernel_size, y_size=y_kernel_size
        )

        mask = k.array > (np.max(k.array) * minimum_response)
        if not auto_resize:
            break

        # Ensure that there be a border of empty pixels, mate. This
        # way we be sure ye larger enough to contain the beam response.
        if (
            np.all(mask[0, :] == 0)
            and np.all(mask[-1, :] == 0)
            and np.all(mask[:, 0] == 0)
            and np.all(mask[-1, :] == 0)
        ):
            break

        kernel_size = tuple(ks + ks * (iteration + 1) for ks in kernel_size)  # type: ignore[assignment]
        logger.warning(f"Increasing to {kernel_size=} for {pixel_scale=}")
    else:
        logger.warning(
            f"{kernel_size=} appears too small for {pixel_scale=} after {iteration=}"
        )

    return mask


def fft_binary_erosion(
    mask: NDArray[np.bool] | NDArray[np.floating],
    kernel: NDArray[np.bool],
) -> NDArray[np.bool]:
    """Attempt to perform a binary erosion using convolution therom. FFT
    the input mask, FFT the input kernel, multiply, FFT the result. This
    would give the running sum of the kernel moved across the image, which
    in the case of boolean-type values will be the number of overlapping 1's.

    The returned mask is after ensuring the kernel total sum matches the running
    sum.

    Args:
        mask (NDArray[np.bool] | NDArray[np.floating]): The mask that will be eroded
        kernel (NDArray[np.bool]): The kernel structure used for the erosion

    Returns:
        NDArray[np.bool]: The eroded mask
    """

    logger.info("Beginning fft erosion ... ")
    original_shape = mask.shape

    mask = mask[..., :, :].astype(float)
    kernel = kernel[..., :, :].astype(float)

    import scipy

    erode_sum = np.floor(
        np.abs(scipy.signal.fftconvolve(mask, kernel, mode="same")) + 0.5
    )

    logger.info("... finished erosion")

    return (erode_sum >= np.sum(kernel)).reshape(original_shape)


def create_multi_scale_erosion(
    mask: NDArray[np.bool],
    fits_header: fits.Header,
    scale: int,
    minimum_response: float = 0.6,
) -> NDArray[np.int32]:
    """Specialised encode per-scale clean masks as bitmapped per-pixel integer values.

    Args:
        mask (np.ndarray): The current mask that will be eroded based on the beam shape
        fits_header (fits.Header): The fits header of the mask used to generate the beam kernel shape
        scales (list[int] | None, optional): Defines the scales that are being used during multi-scale clean. Perform the beam erosion at each of these scales. Defaults to None.
        minimum_response (float, optional): The minimum response of the main restoring beam to craft the shape from. Defaults to 0.6.

    Returns:
        NDArray[np.int32]: _description_
    """
    # Trust no one on the high seas, mate
    if not all([key in fits_header for key in ["BMAJ", "BMIN", "BPA"]]):
        logger.warning(
            "Beam parameters missing. Not performing the beam shape erosion. "
        )
        return mask

    logger.info(f"Eroding with {scale=}, {minimum_response=}")

    # NOTE; The kernel_size=max() is to avoid excessive resize warnings
    # at low scales
    beam_mask_kernel = create_beam_mask_kernel(
        fits_header=fits_header,
        minimum_response=minimum_response,
        pixel_scale=scale,
        kernel_size=max(16, int(1 + scale * 1.1)),
        auto_resize=True,
    )

    # This handles any unsqueezed dimensions
    beam_mask_kernel = beam_mask_kernel.reshape(
        mask.shape[:-2] + beam_mask_kernel.shape
    )

    # As far as this pirate knows the scipy_binary_erosion used
    # at the bottom is evaluated in the imade plane. For larger
    # images and larger kernels (particularly the later), memory
    # usage ballons. I suspect scipy tries to stack all shifted kernels
    # and does the dot product in one go for all positions. In such
    # an instance a better thing to do is via convolution - larger upfront
    # compute cost but no memory explosion.
    size_ratio = np.prod(beam_mask_kernel.shape) / np.prod(mask.shape)
    logger.debug(f"{size_ratio=}")
    fft_erosion_mode = (size_ratio) > 5e-5

    if fft_erosion_mode:
        logger.debug(
            "Will be using fft-based binary erosion, based on size of kernel-to-mask"
        )
        return fft_binary_erosion(mask=mask, kernel=beam_mask_kernel)

    return scipy_binary_erosion(input=mask, iterations=1, structure=beam_mask_kernel)


def beam_shape_erode(
    mask: np.ndarray,
    fits_header: fits.Header,
    minimum_response: float = 0.6,
    scales: list[int] | tuple[int, ...] | None = None,
) -> NDArray[np.bool] | NDArray[np.int32]:
    """Construct a kernel representing the shape of the restoring beam at
        a particular level, and use it as the basis of a binary erosion of the
        input mask.

        The ``fits_header`` is used to construct the beam shape that matches the
        same pixel size

        The ``scales`` parameters outlines the scales being used to clean at during
        some multi-scale deconvolution algorithm. If provided the binary eroision is
        carried out at each scale (the beam is convolved by a circular gaussian at
        this scale). The outputs are stored as a bitmask per pixel where the n'th scales
        is stored as the n'th bit.

        Args:
            mask (np.ndarray): The current mask that will be eroded based on the beam shape
            fits_header (fits.Header): The fits header of the mask used to generate the beam kernel shape
            minimum_response (float, optional): The minimum response of the main restoring beam to craft the shape from. Defaults to 0.6.
            scales (list[int] | tuple[int, ...] | None, optional): Defines the scales that are being used during multi-scale clean. Perform the beam erosion at each of these scales. Defaults to None.

        Returns:
    NDArray[np.bool] | NDArray[np.int32]: The eroded beam shape. If a no/single scale provide it is a bool return, otherwise int32.
    """

    if not all([key in fits_header for key in ["BMAJ", "BMIN", "BPA"]]):
        logger.warning(
            "Beam parameters missing. Not performing the beam shape erosion. "
        )
        return mask

    scales = [0] if scales is None else scales
    assert isinstance(scales, (list, tuple)), f"{scales} should be a list or tuple"
    logger.info(f"Eroding across {scales=}")

    out_mask = np.zeros(mask.shape)

    for index, scale in enumerate(scales):
        erode_mask = create_multi_scale_erosion(
            mask=mask,
            fits_header=fits_header,
            scale=scale,
            minimum_response=minimum_response,
        )
        out_mask[erode_mask == 1.0] += 2**index

    if len(scales) == 1:
        return out_mask.astype(mask.dtype)

    return out_mask


def extract_beam_mask_from_mosaic(
    fits_beam_image_path: Path, fits_mosaic_mask_names: FITSMaskNames
) -> FITSMaskNames:
    """Extract a region based on an existing FITS image from a masked FITS
    image. Here a masked FITS image is intended to be one created by
    ``create_snr_mask_from_fits``.

    A simple cutout (e.g. ``CutOut2D``) might not work as intended, as
    when creating the field image (the intended use case here) might be
    regridded onto a unique pixel grid that does not correspond to one
    that would be constructed by wsclean.

    Args:
        fits_beam_image_path (Path): The template image with a valid WCS. This region is used to extract the masked region
        fits_mosaic_mask_names (FITSMaskNames): The previously masked image

    Returns:
        FITSMaskNames: _description_
    """
    # TODO: Ideally we can accept an arbitrary WCS, or read the wsclean docs to
    # try to construct it ourselves. The last thing that this pirate wants is
    # to run the imager in a dry-run type mode n cleaning type mode purely for
    # the WCS.

    mask_names = create_fits_mask_names(fits_image=fits_beam_image_path)

    with fits.open(fits_beam_image_path) as beam_image:
        header = beam_image[0].header
        beam_image_shape = beam_image[0].data.shape[-2:]

    logger.info(
        f"Will extract {beam_image_shape} from {fits_mosaic_mask_names.mask_fits}"
    )

    with fits.open(fits_mosaic_mask_names.mask_fits) as mosaic_mask:
        logger.info("Extracting region")
        extract_image = reproject_interp(
            input_data=(
                np.squeeze(mosaic_mask[0].data),
                WCS(mosaic_mask[0].header).celestial,
            ),
            output_projection=WCS(header).celestial,
            shape_out=beam_image_shape,
            order=0,
        )
    logger.info("Clipping extract mask results (interpolation errors)")
    extract_image = np.clip(extract_image, 0.0, 1.0)

    fits.writeto(mask_names.mask_fits, extract_image[0].astype(np.int32), header)

    return mask_names


def _get_signal_image(
    image: np.ndarray | None = None,
    rms: np.ndarray | None = None,
    background: np.ndarray | None = None,
    signal: np.ndarray | None = None,
) -> np.ndarray:
    if all([item is None for item in (image, background, rms, signal)]):
        raise ValueError("No input maps have been provided. ")

    if signal is None and image is not None and rms is not None:
        if background is None:
            logger.info("No background supplied, assuming zeros. ")
            background = np.zeros_like(image)

        out_signal = (image - background) / rms
    else:
        out_signal = signal

    return out_signal


def grow_low_snr_mask(
    image: np.ndarray | None = None,
    rms: np.ndarray | None = None,
    background: np.ndarray | None = None,
    signal: np.ndarray | None = None,
    grow_low_snr: float = 2.0,
    grow_low_island_size: int = 512,
    region_mask: np.ndarray | None = None,
) -> np.ndarray:
    """There may be cases where normal thresholding operations based on simple pixel-wise SNR
    cuts fail to pick up diffuse, low surface brightness regions of emission. When some type
    of masking operation is used there may be instances where these regions are never cleaned.

    Sometimes smoothing can help to pick up these features, but when attempting to pass such
    a mask through to the imagery of choice the islands may be larger than the features at their
    native resolution, unless some other more sophisticated filtering is performed.

    This function attempts to grow masks to capture islands of contiguous pixels above a low
    SNR cut that would otherwise go uncleaned.

    Args:
            Args:
        image (Optional[np.ndarray], optional): The total intensity pixels to have the mask for. Defaults to None.
        rms (Optional[np.ndarray], optional): The noise across the image. Defaults to None.
        background (Optional[np.ndarray], optional): The background acros the image. If None, zeros are assumed. Defaults to None.
        signal(Optional[np.ndarray], optional): A signal map. Defaults to None.
        grow_low_snr (float, optional): The SNR pixekls have to be above. Defaults to 2.
        grow_low_island_size (int, optional): The minimum number of pixels an island should be for it to be considered valid. Defaults to 512.
        region_mask (Optional[np.ndarray], optional): Whether some region should be masked out before the island size constraint is applied. Defaults to None.

    Returns:
        np.ndarray: The derived mask of objects with low-surface brightness
    """
    # TODO: The `grow_low_island_size` should be represented in solid angle relative to the restoring beam

    signal = _get_signal_image(
        image=image, rms=rms, background=background, signal=signal
    )

    logger.info(
        f"Growing mask for low surface brightness using {grow_low_snr=} {grow_low_island_size=}"
    )
    low_snr_mask = signal > grow_low_snr
    low_snr_mask = scipy_binary_dilation(
        input=low_snr_mask, iterations=2, structure=np.ones((3, 3))
    )
    low_snr_mask = scipy_binary_erosion(
        input=low_snr_mask, iterations=2, structure=np.ones((3, 3))
    )

    if region_mask is not None:
        low_snr_mask[region_mask] = False

    mask_labels, no_labels = label(low_snr_mask, structure=np.ones((3, 3)))
    _, counts = np.unique(mask_labels.flatten(), return_counts=True)

    small_islands = [
        idx
        for idx, count in enumerate(counts)
        if count < grow_low_island_size and idx > 0
    ]
    low_snr_mask[np.isin(mask_labels, small_islands)] = False

    return low_snr_mask


class SkewResult(NamedTuple):
    positive_pixel_frac: np.ndarray
    """The fraction of positive pixels in a boxcar function"""
    skew_mask: np.ndarray
    """Mask of pixel positions indicating which positions failed the skew test"""
    box_size: int
    """Size of the boxcar window applies"""
    skew_delta: float
    """The test threshold for skew"""


def create_boxcar_skew_mask(
    image: np.ndarray, skew_delta: float, box_size: int
) -> np.ndarray:
    assert 0.0 < skew_delta < 0.5, f"{skew_delta=}, but should be 0.0 to 0.5"
    assert len(image.shape) == 2, (
        f"Expected two dimensions, got image shape of {image.shape}"
    )
    logger.info(f"Computing boxcar skew with {box_size=} and {skew_delta=}")
    positive_pixels = (image > 0.0).astype(np.float32)

    # Counting positive pixel fraction here. The su
    window_shape = (box_size, box_size)
    positive_pixel_fraction = fftconvolve(
        in1=positive_pixels, in2=np.ones(window_shape, dtype=np.float32), mode="same"
    ) / np.prod(window_shape)
    positive_pixel_fraction = np.clip(
        positive_pixel_fraction, 0.0, 1.0
    )  # trust nothing

    skew_mask = positive_pixel_fraction > (0.5 + skew_delta)
    logger.info(f"{np.sum(skew_mask)} pixels above {skew_delta=} with {box_size=}")

    return SkewResult(
        positive_pixel_frac=positive_pixel_fraction,
        skew_mask=skew_mask,
        skew_delta=skew_delta,
        box_size=box_size,
    )


def _minimum_absolute_clip(
    image: np.ndarray, increase_factor: float = 2.0, box_size: int = 100
) -> np.ndarray:
    """Given an input image or signal array, construct a simple image mask by applying a
    rolling boxcar minimum filter, and then selecting pixels above a cut of
    the absolute value value scaled by `increase_factor`. This is a pixel-wise operation.

    Args:
        image (np.ndarray): The input array to consider
        increase_factor (float, optional): How large to scale the absolute minimum by. Defaults to 2.0.
        box_size (int, optional): Size of the rolling boxcar minimum filtr. Defaults to 100.

    Returns:
        np.ndarray: The mask of pixels above the locally varying threshold
    """
    logger.info(f"Minimum absolute clip, {increase_factor=} {box_size=}")
    rolling_box_min = minimum_filter(image, box_size)

    image_mask = image > (increase_factor * np.abs(rolling_box_min))
    # NOTE: This used to attempt to select pixels should that belong to an island of positive pixels with a box that was too small
    # | (
    #     (image > 0.0) & (rolling_box_min > 0.0)
    # )

    return image_mask


def _adaptive_minimum_absolute_clip(
    image: np.ndarray,
    increase_factor: float = 2.0,
    box_size: int = 100,
    adaptive_max_depth: int = 3,
    adaptive_box_step: float = 2.0,
    adaptive_skew_delta: float = 0.2,
) -> np.ndarray:
    logger.info(
        f"Using adaptive minimum absolute clip with {box_size=} {adaptive_skew_delta=}"
    )
    min_value = minimum_filter(input=image, size=box_size)

    for box_round in range(adaptive_max_depth, 0, -1):
        skew_results = create_boxcar_skew_mask(
            image=image, skew_delta=adaptive_skew_delta, box_size=box_size
        )
        if np.all(~skew_results.skew_mask):
            logger.info("No skewed islands detected")
            break
        if any([box_size > dim for dim in image.shape]):
            logger.info(f"{box_size=} larger than a dimension in {image.shape=}")
            break

        logger.info(f"({box_round}) Growing {box_size=} {adaptive_box_step=}")
        box_size = int(box_size * adaptive_box_step)
        _min_value = minimum_filter(input=image, size=box_size)
        logger.debug("Slicing minimum values into place")

        min_value[skew_results.skew_mask] = _min_value[skew_results.skew_mask]

    mask = image > (np.abs(min_value) * increase_factor)

    return mask


def minimum_absolute_clip(
    image: np.ndarray,
    increase_factor: float = 2.0,
    box_size: int = 100,
    adaptive_max_depth: int | None = None,
    adaptive_box_step: float = 2.0,
    adaptive_skew_delta: float = 0.2,
) -> np.ndarray:
    """Implements minimum absolute clip method. A minimum filter of a particular
    boxc size is applied to the input image. The absolute of the output is taken
    and increased by a guard factor, which forms the clipping level used to construct
    a clean mask:

    >>> image > (absolute(minimum_filter(image, box)) * factor)

    The idea is only valid for zero mean and normally distributed pixels, with
    positive definite flux, making it appropriate for Stokes I.

    Larger box sizes and guard factors will make the mask more conservative. Should
    the boxcar be too small relative to some feature it is aligned it is possible
    that an excess of positive pixels will produce an less than optimal clipping
    level. An adaptive box size mode, if activated, attempts to use a larger box
    around these regions.

    The basic idea being detecting regions where the boxcar is too small is around
    the idea that there should be a similar number of positive to negative pixels.
    Should there be too many positive pixels in a region it is likely there is an

    Args:
        image (np.ndarray): Image to create a mask for
        increase_factor (float, optional): The guard factor used to inflate the absolute of the minimum filter. Defaults to 2.0.
        box_size (int, optional): Size of the box car of the minimum filter. Defaults to 100.
        adaptive_max_depth (Optional[int], optional): The maximum number of rounds that the adaptive mode is allowed to perform when rescaling boxcar results in certain directions. Defaults to None.
        adaptive_box_step (float, optional): A multiplicative factor to increase the boxcar size by each round. Defaults to 2.0.
        adaptive_skew_delta (float, optional): Minimum deviation from 0.5 that needs to be met to classify a region as skewed. Defaults to 0.2.

    Returns:
        np.ndarray: Final mask
    """

    if adaptive_max_depth is None:
        return _minimum_absolute_clip(
            image=image, box_size=box_size, increase_factor=increase_factor
        )

    adaptive_max_depth = int(adaptive_max_depth)

    return _adaptive_minimum_absolute_clip(
        image=image,
        increase_factor=increase_factor,
        box_size=box_size,
        adaptive_max_depth=adaptive_max_depth,
        adaptive_box_step=adaptive_box_step,
        adaptive_skew_delta=adaptive_skew_delta,
    )


def _verify_set_positive_seed_clip(
    positive_seed_clip: float, signal: np.ndarray
) -> float:
    """Ensure that the positive seed clip is handled appropriately"""
    max_signal = np.max(signal)
    if max_signal < positive_seed_clip:
        logger.critical(
            f"The maximum signal {max_signal:.4f} is below the provided {positive_seed_clip=}. "
            "Setting clip to 90 percent of maximum. "
        )
        positive_seed_clip = max_signal * 0.9

    return positive_seed_clip


def reverse_negative_flood_fill(
    base_image: np.ndarray,
    masking_options: MaskingOptions,
    pixels_per_beam: float | None = None,
) -> np.ndarray:
    """Attempt to:

    * seed masks around bright regions of an image and grow them to lower significance thresholds
    * remove regions of negative and positive islands that surround bright sources.

    An initial set of islands (and masks) are constructed by first
    using the `positive_seed_clip` to create an initial SNR based
    mask. These islands then are binary dilated to grow the islands
    to adjacent pixels at a lower significance level (see `scipy.ndimage.binary_dilation`).

    Next an attempt is made to remove artefacts around bright sources,  where
    there are likely to be positive and negative artefacts
    that originate from calibration errors, deconvolution errors, or residual
    structure from an incomplete clean.

    This operation will search for islands of _negative_ pixels above a
    threshold. These pixels are then grown after a guard mask has been constructed
    around bright pixels.

    The assumptions that go into this process:

    * the no genuine source of negative sky emission
    * if there are bright negative artefacts there are likely bright positive artefacts nearby
    * such negative pixels are ~10% level artefacts from a presumed bright sources

    Optionally, the `grow_low_snr_mask` may also be considered via the `grow_low_snr` and `grow_low_island_size`
    parameters.

    Args:
        base_image (np.ndarray): The base image or signal map that is used throughout the fill procedure.
        masking_options (MaskingOptions): Options to carry out masking.
        pixels_per_beam (Optional[float], optional): The number of pixels that cover a beam. Defaults to None.

    Returns:
        np.ndarray: Mask of the pixels to clean
    """

    logger.info("Will be reversing flood filling")
    logger.info(f"{masking_options=} ")

    if masking_options.flood_fill_use_mac:
        positive_mask = minimum_absolute_clip(
            image=base_image,
            increase_factor=masking_options.flood_fill_positive_seed_clip,
            box_size=masking_options.flood_fill_use_mac_box_size,
            adaptive_max_depth=masking_options.flood_fill_use_mac_adaptive_max_depth,
            adaptive_box_step=masking_options.flood_fill_use_mac_adaptive_step_factor,
            adaptive_skew_delta=masking_options.flood_fill_use_mac_adaptive_skew_delta,
        )
        flood_floor_mask = minimum_absolute_clip(
            image=base_image,
            increase_factor=masking_options.flood_fill_positive_flood_clip,
            box_size=masking_options.flood_fill_use_mac_box_size,
            adaptive_max_depth=masking_options.flood_fill_use_mac_adaptive_max_depth,
            adaptive_box_step=masking_options.flood_fill_use_mac_adaptive_step_factor,
            adaptive_skew_delta=masking_options.flood_fill_use_mac_adaptive_skew_delta,
        )
    else:
        # Sanity check the upper clip level, you rotten seadog
        positive_seed_clip = _verify_set_positive_seed_clip(
            positive_seed_clip=masking_options.flood_fill_positive_seed_clip,
            signal=base_image,
        )
        # Here we create the mask image that will start the binary dilation
        # process, and we will ensure only pixels above the `positive_flood_clip`
        # are allowed to be dilated. In other words we are growing the mask
        positive_mask = base_image >= positive_seed_clip
        flood_floor_mask = base_image > masking_options.flood_fill_positive_flood_clip

    positive_dilated_mask = scipy_binary_dilation(
        input=positive_mask,
        mask=flood_floor_mask,
        iterations=1000,
        structure=np.ones((3, 3)),
    )

    if masking_options.grow_low_snr_island:
        low_snr_mask = grow_low_snr_mask(
            signal=base_image,
            grow_low_snr=masking_options.grow_low_snr_island_clip,
            grow_low_island_size=masking_options.grow_low_snr_island_size,
        )
        positive_dilated_mask[low_snr_mask] = True

    return positive_dilated_mask.astype(np.int32)


def _create_signal_from_rmsbkg(
    image: Path | np.ndarray,
    rms: Path | np.ndarray,
    bkg: Path | np.ndarray,
) -> np.ndarray:
    logger.info("Creating signal image")

    if isinstance(image, Path):
        with fits.open(image) as in_fits:
            logger.info(f"Loading {image}")
            image = in_fits[0].data  # type: ignore

    assert isinstance(image, np.ndarray), (
        f"Expected the image to be a numpy array by now, instead have {type(image)}"
    )

    if isinstance(bkg, Path):
        with fits.open(bkg) as in_fits:
            logger.info(f"Loading {bkg=}")
            bkg = in_fits[0].data  # type: ignore

    logger.info("Subtracting background")
    image -= bkg

    if isinstance(rms, Path):
        with fits.open(rms) as in_fits:
            logger.info(f"Loading {rms=}")
            rms = in_fits[0].data  # type: ignore

    logger.info("Dividing by rms")
    image /= rms

    return np.array(image)


def _need_to_make_signal(masking_options: MaskingOptions) -> bool:
    """Isolated functions to consider whether a signal image is needed"""
    return not masking_options.flood_fill_use_mac


# TODO: Need to rename this function
# TODO: This should be made into a dispatcher type function
def create_snr_mask_from_fits(
    fits_image_path: Path,
    masking_options: MaskingOptions,
    fits_rms_path: Path | None,
    fits_bkg_path: Path | None,
    create_signal_fits: bool = False,
    overwrite: bool = True,
) -> FITSMaskNames:
    """Create a mask for an input FITS image based on a signal to noise given a corresponding pair of RMS and background FITS images.

    Internally should a signal image be needed it is computed as something akin to:
    > signal = (image - background) / rms

    This is done in a staged manner to minimise the number of (potentially large) images
    held in memory.

    Each of the input images needs to share the same shape. This means that compression
    features offered by some tooling (e.g. BANE --compress) can not be used.

    Depending on the `MaksingOptions` used the signal image may not be needed.

    Once the signal map as been computed, all pixels below ``min_snr`` are flagged.

    Args:
        fits_image_path (Path): Path to the FITS file containing an image
        masking_options (MaskingOptions): Configurables on the masking operation procedure.
        fits_rms_path (Optional[Path], optional): Path to the FITS file with an RMS image corresponding to ``fits_image_path``. Defaults to None.
        fits_bkg_path (Optional[Path], optional): Path to the FITS file with an background image corresponding to ``fits_image_path``. Defaults to None.
        create_signal_fits (bool, optional): Create an output signal map. Defaults to False.
        overwrite (bool): Passed to `fits.writeto`, and will overwrite files should they exist. Defaults to True.

    Returns:
        FITSMaskNames: Container describing the signal and mask FITS image paths. If ``create_signal_path`` is None, then the ``signal_fits`` attribute will be None.
    """
    # Short cut to the convolve first
    if masking_options.convolve_first:
        logger.warning(
            "Using the convolve then erode algorithm, some options may be ignored"
        )
        return create_convolved_erosion_mask(
            fits_image_path=fits_image_path, masking_options=masking_options
        )

    mask_names = create_fits_mask_names(
        fits_image=fits_image_path, include_signal_path=create_signal_fits
    )

    # TODOL Make the bkg and rms images optional. Don't need to load if mac is usede
    with fits.open(fits_image_path) as fits_image:
        fits_header = fits_image[0].header  # type: ignore
        signal_data = fits_image[0].data  # type: ignore

    if _need_to_make_signal(masking_options=masking_options):
        assert isinstance(fits_rms_path, Path) and isinstance(fits_bkg_path, Path), (
            "Expected paths for input RMS and bkg FITS files"
        )
        signal_data = _create_signal_from_rmsbkg(
            image=signal_data, rms=fits_rms_path, bkg=fits_bkg_path
        )

        if create_signal_fits:
            logger.info(f"Writing {mask_names.signal_fits}")
            fits.writeto(
                filename=mask_names.signal_fits,
                data=signal_data,
                header=fits_header,
                overwrite=overwrite,
            )

    pixels_per_beam = get_pixels_per_beam(fits_path=fits_image_path)

    # Following the help in wsclean:
    # WSClean accepts masks in CASA format and in fits file format. A mask is a
    # normal, single polarization image file, where all zero values are interpreted
    # as being not masked, and all non-zero values are interpreted as masked. In the
    # case of a fits file, the file may either contain a single frequency or it may
    # contain a cube of images.
    if masking_options.flood_fill:
        # TODO: The image and signal masks both don't need to be inputs. Image is only used
        # if mac = True
        mask_data = reverse_negative_flood_fill(
            base_image=np.squeeze(signal_data),
            masking_options=masking_options,
            pixels_per_beam=pixels_per_beam,
        )
        mask_data = mask_data.reshape(signal_data.shape)
    else:
        logger.info(f"Clipping using a {masking_options.base_snr_clip=}")
        mask_data = (signal_data > masking_options.base_snr_clip).astype(np.int32)

    if masking_options.beam_shape_erode:
        mask_data = beam_shape_erode(
            mask=mask_data,
            fits_header=fits_header,
            minimum_response=masking_options.beam_shape_erode_minimum_response,
            scales=masking_options.beam_shape_erode_scales,
        )

    logger.info(f"Writing {mask_names.mask_fits}")
    fits.writeto(
        filename=mask_names.mask_fits,
        data=mask_data,
        header=fits_header,
        overwrite=overwrite,
    )

    return mask_names


def convolve_image_by_scale(
    image_data: NDArray[np.floating], scale: int
) -> NDArray[np.floating]:
    """Generate a convolved version of the input image based on some convolved scale.
    Note that here the scale refers to the scales used by wsclean. Per the multiscale
    documentation, where considering gaussians:

    >> fwhm = 0.45 * scale

    Also not that:

    >> fwhm = 2.355 * sigma

    Args:
        image_data (NDArray[np.floating]): The imaged to be convolved
        scale (int): The pixel scale to smooth with

    Returns:
        NDArray[np.floating]: Smoother version of the input image
    """

    from scipy.ndimage import maximum_filter, minimum_filter

    logger.info("Computing the open filter image")
    min_image = minimum_filter(image_data, scale)
    image_data = maximum_filter(min_image, scale)

    logger.info(f"Convoling with {scale=}")

    # wsclean scales are converted to a fwhm as 0.45 * pixel scales. To convert
    # the FWHM to a sigma we divided by 2.355, matttee
    fwhm = 0.45 * scale
    sigma = fwhm / 2.355

    logger.info(f"Generating gaussian kernel for {scale=} {fwhm=:.3f} {sigma=:.3f}")

    pix_sigma = int(sigma * 5)
    x = np.linspace(0, pix_sigma, pix_sigma)
    y = np.linspace(0, pix_sigma, pix_sigma)

    x -= x[-1] / 2
    y -= y[-1] / 2

    # Form the 2d image gride here, ya sea dog
    distance_squared = x[None, :] ** 2 + y[:, None] ** 2
    kernel = np.exp(-(distance_squared / (2 * sigma**2)))

    # Make sure the ships match dimensions
    kernel = kernel.reshape(image_data.shape[:-2] + kernel.shape)
    logger.info(f"{image_data.shape=} {kernel.shape=}")
    return fftconvolve(image_data, kernel, mode="same")


# TODO: This needs some specific unit tests
def create_convolved_erosion_mask(
    fits_image_path: Path,
    masking_options: MaskingOptions,
) -> FITSMaskNames:
    """Create a per-scale clean mask by smoothing the input image to some scale, and then
    applying a reverse flood fill with the minimum absolute clip statistic. After the initial
    mask per scale is constructed a binary erosion is then performed to further refine the mask.

    Args:
        fits_image_path (Path): The path to the FITS image to load.
        masking_options (MaskingOptions): Options to use throughout the convolve and erosion process.

    Returns:
        FITSMaskNames: Clean mask written to a FITS file. Should multiple scales be specified they are encoded bitwise per pixel.
    """
    mask_names = create_fits_mask_names(fits_image=fits_image_path)

    logger.info(f"Getting image data from {fits_image_path=}")
    base_image = fits.getdata(fits_image_path)
    fits_header = fits.getheader(fits_image_path)

    original_shape = base_image.shape

    base_image = np.squeeze(base_image)
    assert len(base_image.shape) == 2, (
        f"The shape of data is {original_shape}, and can not be converted to a two-dimensional image"
    )

    scales = (
        [0]
        if masking_options.beam_shape_erode_scales is None
        else masking_options.beam_shape_erode_scales
    )
    logger.info(f"Will be processing {scales=}")

    output_mask = np.zeros_like(base_image)

    for index, scale in enumerate(scales):
        convolved_image = base_image.copy()
        logger.info(f"Processing {scale=}")

        # Image at native scale does not need to be convolved
        if scale > 0:
            convolved_image = convolve_image_by_scale(
                image_data=convolved_image, scale=scale
            )

            # fits.writeto(
            #     fits_image_path.with_suffix(f".minmaxcon-{scale}.fits"),
            #     data=convolved_image,
            #     header=fits_header,
            #     overwrite=True,
            # )

        box_size = masking_options.flood_fill_use_mac_box_size + scale

        positive_mask = minimum_absolute_clip(
            image=convolved_image,
            increase_factor=masking_options.flood_fill_positive_seed_clip,
            box_size=box_size,
            adaptive_max_depth=masking_options.flood_fill_use_mac_adaptive_max_depth,
            adaptive_box_step=masking_options.flood_fill_use_mac_adaptive_step_factor,
            adaptive_skew_delta=masking_options.flood_fill_use_mac_adaptive_skew_delta,
        )
        flood_fill_positive_flood_clip = masking_options.flood_fill_positive_flood_clip
        if scale == 0 and flood_fill_positive_flood_clip < 0.8:
            logger.warning(
                f"{flood_fill_positive_flood_clip=}, too low for {scale=}. Setting to 0.8"
            )
            flood_fill_positive_flood_clip = 0.8

        flood_floor_mask = minimum_absolute_clip(
            image=convolved_image,
            increase_factor=flood_fill_positive_flood_clip,
            box_size=box_size,
            adaptive_max_depth=masking_options.flood_fill_use_mac_adaptive_max_depth,
            adaptive_box_step=masking_options.flood_fill_use_mac_adaptive_step_factor,
            adaptive_skew_delta=masking_options.flood_fill_use_mac_adaptive_skew_delta,
        )
        flood_floor_mask = binary_fill_holes(flood_floor_mask)

        # Ensure positive only pixels. useful (necessary?) at larger scales bridging
        # across negative pixels
        positive_dilated_mask = scipy_binary_dilation(
            input=positive_mask,
            mask=flood_floor_mask,
            iterations=1000,
            structure=np.ones((3, 3)),
        )

        # output_mask[positive_dilated_mask] += 2**index

        scale_mask = (
            create_multi_scale_erosion(
                mask=positive_dilated_mask,
                fits_header=fits_header,
                scale=scale,
                minimum_response=masking_options.beam_shape_erode_minimum_response,
            )
            == 1
        )

        output_mask[scale_mask] += 2**index

    logger.info(f"Writing {mask_names.scale_mask_fits}")
    fits.writeto(
        mask_names.scale_mask_fits,
        header=fits_header,
        data=output_mask.reshape(original_shape),
        overwrite=True,
    )
    return mask_names


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Simple utility functions to create masks from FITS images. "
    )

    subparser = parser.add_subparsers(
        dest="mode", help="Operation mode of flint_bandpass"
    )

    fits_parser = subparser.add_parser(
        "mask",
        help="Create a mask for an image, potentially using its RMS and BKG images (e.g. outputs from BANE). Output FITS image will default to the image with a mask suffix.",
    )
    fits_parser.add_argument("image", type=Path, help="Path to the input image. ")
    fits_parser = add_options_to_parser(
        parser=fits_parser, options_class=MaskingOptions
    )

    fits_parser.add_argument(
        "--rms-fits", type=Path, help="Path to the RMS of the input image. "
    )
    fits_parser.add_argument(
        "--bkg-fits", type=Path, help="Path to the BKG of the input image. "
    )
    fits_parser.add_argument(
        "--save-signal",
        action="store_true",
        help="Save the signal image internally generated (should it be generated)",
    )

    extract_parser = subparser.add_parser(
        "extractmask",
        help="Extract a beam FITS masked region from a larger FITS mask mosaic image. ",
    )

    extract_parser.add_argument(
        "beam_image",
        type=Path,
        help="The FITS image with the WCS that will be used to extract the mask region from",
    )

    extract_parser.add_argument(
        "mosaic_image",
        type=Path,
        help="The field mosaic image that will be used as a basis to extract a region mask from",
    )

    return parser


def cli():
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "mask":
        masking_options = create_options_from_parser(
            parser_namespace=args, options_class=MaskingOptions
        )
        if masking_options.convolve_first:
            create_convolved_erosion_mask(
                fits_image_path=args.image, masking_options=masking_options
            )
        else:
            create_snr_mask_from_fits(
                fits_image_path=args.image,
                fits_rms_path=args.rms_fits,
                fits_bkg_path=args.bkg_fits,
                create_signal_fits=args.save_signal,
                masking_options=masking_options,
            )

    elif args.mode == "extractmask":
        extract_beam_mask_from_mosaic(
            fits_beam_image_path=args.beam_image,
            fits_mosaic_mask_names=FITSMaskNames(mask_fits=args.mosaic_image),
        )
    else:
        logger.error(f"Supplied mode {args.mode} is not known. ")


if __name__ == "__main__":
    cli()
