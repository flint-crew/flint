"""Split a clean mask into islands that are each imaged on their own"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from eye_patch.naming import MASK_SUFFIX, SCALE_MASK_SUFFIX, FITSMaskNames
from eye_patch.scales import flatten_scale_mask
from numpy.typing import NDArray
from scipy.ndimage import binary_dilation, find_objects, label, maximum

from flint.logging import logger
from flint.options import BaseOptions


class CubletRegion(BaseOptions):
    """A square window around one island of a larger image"""

    idx: int
    """Label of the island"""
    centre: tuple[int, int]
    """The (y, x) pixel of the larger image at the centre of the window"""
    size: int
    """Width of the window in pixels"""
    shift: tuple[str, str]
    """RA and Dec of the centre pixel, as wsclean reads them"""
    super_weight: float
    """Window size over the size of the larger image, which keeps its weighting"""
    peak: float
    """Brightest pixel of the island"""
    fits_mask: FITSMaskNames | None = None
    """Clean masks of the window, holding this island alone"""


def round_threshold(
    peak: float, round_gain: float, round_number: int, floor: float = 0.0
) -> float:
    """The level every island is cleaned to in a round, so none outruns the rest"""
    return max(peak * (1.0 - round_gain) ** round_number, floor)


def label_islands(mask: NDArray, merge_pixels: int = 0) -> NDArray[np.int32]:
    """Label the islands of a mask, treating those within ``2 * merge_pixels`` as one"""
    active = flatten_scale_mask(scale_mask=mask) > 0
    grouped = (
        binary_dilation(active, structure=np.ones((3, 3)), iterations=merge_pixels)
        if merge_pixels > 0
        else active
    )
    labels, count = label(grouped, structure=np.ones((3, 3)))
    labels[~active] = 0
    logger.info(f"Found {count} islands")
    return labels.astype(np.int32)


def _window_size(extent: int, pad_factor: float, min_size: int) -> int:
    size = max(int(np.ceil(extent * pad_factor)), min_size)
    return size + size % 2


def create_cublet_regions(
    labels: NDArray[np.int32],
    image: NDArray[np.floating],
    header: fits.Header,
    pad_factor: float = 2.5,
    min_size: int = 128,
    max_islands: int | None = None,
) -> list[CubletRegion]:
    """Make the window of each island, brightest first"""
    assert labels.shape == image.shape, f"{labels.shape=} differs from {image.shape=}"
    assert image.shape[0] == image.shape[1], f"Image is not square, {image.shape=}"

    count = int(labels.max())
    if count == 0:
        return []

    indices = np.arange(1, count + 1)
    peaks = maximum(np.nan_to_num(image), labels=labels, index=indices)
    wcs = WCS(header).celestial

    regions = []
    for idx, box, peak in zip(indices, find_objects(labels), peaks, strict=True):
        if box is None:
            continue
        y_slice, x_slice = box
        centre = (
            (y_slice.start + y_slice.stop) // 2,
            (x_slice.start + x_slice.stop) // 2,
        )
        extent = max(y_slice.stop - y_slice.start, x_slice.stop - x_slice.start)
        size = _window_size(extent=extent, pad_factor=pad_factor, min_size=min_size)
        position = wcs.pixel_to_world(centre[1], centre[0])
        regions.append(
            CubletRegion(
                idx=int(idx),
                centre=centre,
                size=size,
                shift=(
                    position.ra.to_string(unit=u.hourangle, sep="hms", precision=6),
                    position.dec.to_string(unit=u.deg, sep="dms", precision=5),
                ),
                super_weight=size / image.shape[0],
                peak=float(peak),
            )
        )

    regions.sort(key=lambda region: region.peak, reverse=True)
    return regions[:max_islands]


def island_cutout(
    mask: NDArray, labels: NDArray[np.int32], region: CubletRegion
) -> NDArray:
    """Cut the window of an island from a mask, keeping only that island"""
    cutout = np.zeros((region.size, region.size), dtype=mask.dtype)
    starts = [centre - region.size // 2 for centre in region.centre]
    # The window may run past the edge of the mask
    source = tuple(
        slice(max(start, 0), min(start + region.size, length))
        for start, length in zip(starts, mask.shape, strict=True)
    )
    target = tuple(
        slice(part.start - start, part.stop - start)
        for part, start in zip(source, starts, strict=True)
    )
    cutout[target] = np.where(labels[source] == region.idx, mask[source], 0)
    return cutout


def _window_header(header: fits.Header, region: CubletRegion) -> fits.Header:
    window_header = header.copy()
    window_header["CRPIX1"] -= region.centre[1] - region.size // 2
    window_header["CRPIX2"] -= region.centre[0] - region.size // 2
    return window_header


def write_island_masks(
    regions: list[CubletRegion],
    labels: NDArray[np.int32],
    mask_names: FITSMaskNames,
    output_dir: Path,
) -> list[CubletRegion]:
    """Write the clean masks of each window, and attach them to the regions"""
    output_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, list[Path]] = {}
    for field, suffix in (
        ("mask_fits", MASK_SUFFIX),
        ("scale_mask_fits", SCALE_MASK_SUFFIX),
    ):
        mask_path = getattr(mask_names, field)
        if mask_path is None:
            continue
        with fits.open(mask_path) as mask_fits:
            header = mask_fits[0].header
            mask = np.squeeze(mask_fits[0].data)
        stem = mask_path.name.removesuffix(suffix)
        written[field] = []
        for region in regions:
            island_path = output_dir / f"{stem}.island{region.idx:05d}{suffix}"
            fits.writeto(
                island_path,
                data=island_cutout(mask=mask, labels=labels, region=region),
                header=_window_header(header=header, region=region),
                overwrite=True,
            )
            written[field].append(island_path)

    return [
        region.with_options(
            fits_mask=FITSMaskNames(
                **{field: paths[index] for field, paths in written.items()}
            )
        )
        for index, region in enumerate(regions)
    ]


def find_cublet_regions(
    image_fits: Path,
    mask_names: FITSMaskNames,
    output_dir: Path,
    merge_pixels: int = 0,
    pad_factor: float = 2.5,
    min_size: int = 128,
    max_islands: int | None = None,
) -> list[CubletRegion]:
    """Find the islands of a mask of an image, and write the clean masks of their windows"""
    mask_path = mask_names.scale_mask_fits or mask_names.mask_fits
    assert mask_path is not None, "A mask is needed to find islands"

    with fits.open(mask_path) as mask_fits:
        labels = label_islands(
            mask=np.squeeze(mask_fits[0].data), merge_pixels=merge_pixels
        )
    with fits.open(image_fits) as image:
        regions = create_cublet_regions(
            labels=labels,
            image=np.squeeze(image[0].data),
            header=image[0].header,
            pad_factor=pad_factor,
            min_size=min_size,
            max_islands=max_islands,
        )
    logger.info(f"Kept {len(regions)} islands")

    return write_island_masks(
        regions=regions, labels=labels, mask_names=mask_names, output_dir=output_dir
    )
