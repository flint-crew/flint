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
from scipy.spatial import cKDTree

from flint.logging import logger
from flint.options import BaseOptions


class CubletRegion(BaseOptions):
    """A square window around an island of a larger image, or around a patch of
    islands that are imaged together"""

    idx: int
    """Label of the island or patch"""
    centre: tuple[int, int]
    """The (y, x) pixel of the larger image at the centre of the window"""
    size: int
    """Width of the window in pixels"""
    shift: tuple[str, str]
    """RA and Dec of the centre pixel, as wsclean reads them"""
    super_weight: float
    """Padded window size over the padded size of the larger image, which keeps its weighting"""
    peak: float
    """Largest absolute pixel of the island, so that an island cleaned too far counts as unfinished"""
    fits_mask: FITSMaskNames | None = None
    """Clean masks of the window, holding only what has this label"""


def round_threshold(
    peak: float, round_gain: float, round_number: int, floor: float = 0.0
) -> float:
    """The level every island is cleaned to in a round, so none outruns the rest"""
    return max(peak * (1.0 - round_gain) ** round_number, floor)


def masked_peak(image_fits: Path, mask_fits: Path) -> float:
    """The largest absolute pixel of an image inside a mask"""
    with fits.open(image_fits) as image, fits.open(mask_fits) as mask:
        data = np.nan_to_num(np.squeeze(image[0].data))
        masked = np.squeeze(mask[0].data) != 0
    return float(np.max(np.abs(data[..., masked]), initial=0.0))


def union_masks(
    mask_names: FITSMaskNames, earlier_mask_names: FITSMaskNames, output_dir: Path
) -> FITSMaskNames:
    """Add the islands of an earlier mask to a mask, so they can still be cleaned.

    Bit n of a scale mask is scale n, so the union is of bits.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    union: dict[str, Path] = {}
    for field in ("mask_fits", "scale_mask_fits"):
        mask_path = getattr(mask_names, field)
        earlier_path = getattr(earlier_mask_names, field)
        assert (mask_path is None) == (earlier_path is None), (
            f"Only one of the masks has a {field}"
        )
        if mask_path is None:
            continue
        with fits.open(mask_path) as mask, fits.open(earlier_path) as earlier:
            header = mask[0].header
            data = mask[0].data
            earlier_data = earlier[0].data
        assert data.shape == earlier_data.shape, (
            f"Masks differ in shape, {data.shape=} and {earlier_data.shape=}"
        )
        joined = np.nan_to_num(data).astype(np.int64) | np.nan_to_num(
            earlier_data
        ).astype(np.int64)
        union[field] = output_dir / mask_path.name
        fits.writeto(
            union[field], data=joined.astype(data.dtype), header=header, overwrite=True
        )

    return FITSMaskNames(**union)


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


def _window_size(extent: int, largest: int, pad_factor: float, min_size: int) -> int:
    """Width of the window around islands that span ``extent`` pixels, padded for
    the ``largest`` of them. For one island that is ``pad_factor`` times its extent."""
    size = max(int(np.ceil(extent + largest * (pad_factor - 1))), min_size)
    return size + size % 2


def _extent(box: tuple[int, int, int, int]) -> int:
    return max(box[1] - box[0], box[3] - box[2])


def island_boxes(labels: NDArray[np.int32]) -> list[tuple[int, int, int, int]]:
    """The (y start, y stop, x start, x stop) of the box around each label, in label order"""
    boxes = find_objects(labels)
    assert None not in boxes, "A label has no pixels"
    return [(y.start, y.stop, x.start, x.stop) for y, x in boxes]


def group_islands(
    boxes: list[tuple[int, int, int, int]],
    patch_area: int,
    patch_max_size: int,
    pad_factor: float = 2.5,
    min_size: int = 128,
) -> list[int]:
    """Group islands into patches that are cheaper to image than the islands
    alone, returning the patch of each island.

    Pairs of islands are taken closest first. Their patches are joined when the
    window of the two together is no wider than ``patch_max_size``, and has
    fewer than ``patch_area`` pixels more than their two windows. It is a quick
    pass and not the best grouping there is.
    """
    if len(boxes) < 2:
        return list(range(len(boxes)))

    centres = [((box[0] + box[1]) / 2, (box[2] + box[3]) / 2) for box in boxes]
    # Islands further apart than this can not share a window
    pairs = cKDTree(centres).query_pairs(r=patch_max_size, p=np.inf)

    def gap(pair: tuple[int, int]) -> int:
        a, b = boxes[pair[0]], boxes[pair[1]]
        return max(a[0] - b[1], b[0] - a[1], a[2] - b[3], b[2] - a[3], 0)

    parent = list(range(len(boxes)))
    patch_boxes = list(boxes)
    largest = [_extent(box) for box in boxes]

    def root(island: int) -> int:
        while parent[island] != island:
            parent[island] = parent[parent[island]]
            island = parent[island]
        return island

    def size(patch: int) -> int:
        return _window_size(
            extent=_extent(patch_boxes[patch]),
            largest=largest[patch],
            pad_factor=pad_factor,
            min_size=min_size,
        )

    for pair in sorted(pairs, key=gap):
        a, b = root(pair[0]), root(pair[1])
        if a == b:
            continue
        box_a, box_b = patch_boxes[a], patch_boxes[b]
        joined_box = (
            min(box_a[0], box_b[0]),
            max(box_a[1], box_b[1]),
            min(box_a[2], box_b[2]),
            max(box_a[3], box_b[3]),
        )
        joined_largest = max(largest[a], largest[b])
        joined_size = _window_size(
            extent=_extent(joined_box),
            largest=joined_largest,
            pad_factor=pad_factor,
            min_size=min_size,
        )
        if joined_size > patch_max_size:
            continue
        if joined_size**2 - size(a) ** 2 - size(b) ** 2 >= patch_area:
            continue
        parent[b] = a
        patch_boxes[a] = joined_box
        largest[a] = joined_largest

    roots = [root(island) for island in range(len(boxes))]
    patch_of_root = {patch: index for index, patch in enumerate(dict.fromkeys(roots))}
    return [patch_of_root[patch] for patch in roots]


def label_patches(
    labels: NDArray[np.int32],
    patch_area: int,
    patch_max_size: int,
    pad_factor: float = 2.5,
    min_size: int = 128,
) -> tuple[NDArray[np.int32], list[int]]:
    """Relabel islands by the patch they are imaged in (see ``group_islands``).
    Also returns the extent of the largest island of each patch, in label order."""
    boxes = island_boxes(labels=labels)
    patches = group_islands(
        boxes=boxes,
        patch_area=patch_area,
        patch_max_size=patch_max_size,
        pad_factor=pad_factor,
        min_size=min_size,
    )
    count = max(patches, default=-1) + 1
    logger.info(f"Grouped {len(boxes)} islands into {count} patches")

    largest = [0] * count
    for box, patch in zip(boxes, patches, strict=True):
        largest[patch] = max(largest[patch], _extent(box))

    patch_labels = np.array([0] + [patch + 1 for patch in patches], dtype=np.int32)
    return patch_labels[labels], largest


def create_cublet_regions(
    labels: NDArray[np.int32],
    image: NDArray[np.floating],
    header: fits.Header,
    pad_factor: float = 2.5,
    min_size: int = 128,
    max_islands: int | None = None,
    max_size: int | None = None,
    padding: float = 1.2,
    largest: list[int] | None = None,
) -> list[CubletRegion]:
    """Make the window of each island, brightest first. Islands whose window
    would be larger than ``max_size`` are left out.

    ``padding`` is the wsclean ``-padding`` both images are made with, as wsclean
    sizes its weight grid from the padded image.

    ``largest`` is the extent of the largest island of each label, for labels
    that are patches of islands (see ``label_patches``).
    """
    assert labels.shape == image.shape, f"{labels.shape=} differs from {image.shape=}"
    assert image.shape[0] == image.shape[1], f"Image is not square, {image.shape=}"

    count = int(labels.max())
    if count == 0:
        return []

    indices = np.arange(1, count + 1)
    peaks = maximum(np.abs(np.nan_to_num(image)), labels=labels, index=indices)
    wcs = WCS(header).celestial

    boxes = island_boxes(labels=labels)
    if largest is None:
        largest = [_extent(box) for box in boxes]

    regions = []
    for idx, box, pad_for, peak in zip(indices, boxes, largest, peaks, strict=True):
        centre = ((box[0] + box[1]) // 2, (box[2] + box[3]) // 2)
        size = _window_size(
            extent=_extent(box),
            largest=pad_for,
            pad_factor=pad_factor,
            min_size=min_size,
        )
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
                super_weight=np.ceil(size * padding)
                / np.ceil(image.shape[0] * padding),
                peak=float(peak),
            )
        )

    if max_size is not None:
        too_large = [region.size for region in regions if region.size > max_size]
        if too_large:
            logger.warning(
                f"Ignoring {len(too_large)} islands with windows over {max_size=} pixels: {sorted(too_large)}"
            )
        regions = [region for region in regions if region.size <= max_size]

    regions.sort(key=lambda region: region.peak, reverse=True)
    return regions[:max_islands]


def _window_slices(region: CubletRegion, shape: tuple[int, ...]) -> tuple[slice, slice]:
    """The part of an image of this shape that the window covers"""
    starts = [centre - region.size // 2 for centre in region.centre]
    # The window may run past the edge of the image
    y_slice, x_slice = (
        slice(max(start, 0), min(start + region.size, length))
        for start, length in zip(starts, shape, strict=True)
    )
    return y_slice, x_slice


def count_shared_windows(regions: list[CubletRegion], labels: NDArray[np.int32]) -> int:
    """How many windows hold pixels of a label that is not their own. Those
    pixels are in view of the window but are not cleaned in it."""
    shared = 0
    for region in regions:
        in_view = np.unique(labels[_window_slices(region=region, shape=labels.shape)])
        shared += bool(np.any((in_view != 0) & (in_view != region.idx)))
    return shared


def write_patch_map(
    regions: list[CubletRegion],
    labels: NDArray[np.int32],
    header: fits.Header,
    output_path: Path,
) -> Path:
    """Write an image to look at the windows with. The islands of each region
    and the outline of its window are set to its label, and all else is 0."""
    # Halves the file where the labels allow it, as the image is as large as the beam
    dtype = np.int16 if labels.max() < 2**15 else np.int32
    kept = np.zeros(int(labels.max()) + 1, dtype=dtype)
    for region in regions:
        kept[region.idx] = region.idx
    patch_map = kept[labels]

    border = 2
    for region in regions:
        y_slice, x_slice = _window_slices(region=region, shape=labels.shape)
        window = patch_map[y_slice, x_slice]
        for edge in (
            window[:border],
            window[-border:],
            window[:, :border],
            window[:, -border:],
        ):
            edge[...] = region.idx

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(
        output_path,
        data=patch_map,
        header=WCS(header).celestial.to_header(),
        overwrite=True,
    )
    return output_path


def island_cutout(
    mask: NDArray, labels: NDArray[np.int32], region: CubletRegion
) -> NDArray:
    """Cut the window of an island from a mask, keeping only that island"""
    cutout = np.zeros((region.size, region.size), dtype=mask.dtype)
    starts = [centre - region.size // 2 for centre in region.centre]
    source = _window_slices(region=region, shape=mask.shape)
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
    max_size: int | None = None,
    patch_area: int | None = None,
    patch_max_size: int = 1024,
) -> list[CubletRegion]:
    """Find the islands of a mask of an image, and write the clean masks of their
    windows. With ``patch_area`` nearby islands share a window (see ``group_islands``).

    An image of the windows is written beside the masks (see ``write_patch_map``).
    """
    mask_path = mask_names.scale_mask_fits or mask_names.mask_fits
    assert mask_path is not None, "A mask is needed to find islands"

    with fits.open(mask_path) as mask_fits:
        labels = label_islands(
            mask=np.squeeze(mask_fits[0].data), merge_pixels=merge_pixels
        )

    largest = None
    if patch_area is not None and labels.max() > 0:
        labels, largest = label_patches(
            labels=labels,
            patch_area=patch_area,
            patch_max_size=patch_max_size,
            pad_factor=pad_factor,
            min_size=min_size,
        )

    with fits.open(image_fits) as image:
        header = image[0].header
        regions = create_cublet_regions(
            labels=labels,
            image=np.squeeze(image[0].data),
            header=header,
            pad_factor=pad_factor,
            min_size=min_size,
            max_islands=max_islands,
            max_size=max_size,
            largest=largest,
        )
    logger.info(f"Kept {len(regions)} islands or patches")
    logger.info(
        f"{count_shared_windows(regions=regions, labels=labels)} of their windows hold part of another"
    )
    write_patch_map(
        regions=regions,
        labels=labels,
        header=header,
        output_path=output_dir
        / f"{image_fits.name.removesuffix('.fits')}.patches.fits",
    )

    return write_island_masks(
        regions=regions, labels=labels, mask_names=mask_names, output_dir=output_dir
    )
