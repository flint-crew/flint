"""The images and auxiliary products made by an imager, and operations on sets of them"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from capn_crunch import options_to_dict

from flint.cube import combine_images_to_cube
from flint.exceptions import NamingException
from flint.logging import logger
from flint.naming import (
    ProcessedNameComponents,
    extract_components_from_name,
    pol_name_token,
    replace_name_field,
    split_images,
)
from flint.options import BaseOptions, FitsCubeOptions
from flint.utils import remove_files_folders


class ImageSet(BaseOptions):
    """A structure to represent the images and auxiliary products produced by an imager"""

    prefix: str
    """Prefix of the images and other output products. For wsclean this is its -name argument"""
    image: list[Path]
    """Images produced. """
    psf: list[Path] | None = None
    """References to the PSFs produced by the imager. """
    dirty: list[Path] | None = None
    """Dirty images. """
    model: list[Path] | None = None
    """Model images.  """
    residual: list[Path] | None = None
    """Residual images."""
    source_list: Path | None = None
    """Path to a source list that accompanies the image data"""


def merge_image_sets(
    image_sets: list[ImageSet],
) -> ImageSet:
    """Merge the image sets from multiple wsclean results into a single image set.

    Args:
        wsclean_results (List[WSCleanResult]): The results of multiple wsclean imaging runs

    Returns:
        ImageSet: The merged image set
    """
    logger.info(f"Merging {len(image_sets)} image sets")

    prefix = image_sets[0].prefix
    for image_set in image_sets:
        if image_set.prefix != prefix:
            msg = f"Image sets have different prefixes - will use the first prefix {prefix}"
            logger.warning(msg)

    image_set_dict: dict[str, Any] = {}
    for image_set in image_sets:
        for key, value in image_set._asdict().items():
            if key == "prefix":
                continue
            if value is None:
                continue
            if key not in image_set_dict:
                image_set_dict[key] = []
            if isinstance(value, (list, tuple)):
                image_set_dict[key].extend(value)
            else:
                image_set_dict[key].append(value)

    image_set_dict["prefix"] = prefix

    logger.info(f"Merged image set: {image_set_dict=}")

    source_lists = image_set_dict.get("source_list", None)
    if (
        source_lists is not None
        and isinstance(source_lists, list)
        and len(source_lists) > 1
    ):
        msg = f"Multiple source lists found in merged image set: {source_lists}. Only the first will be used."
        logger.warning(msg)
        image_set_dict["source_list"] = source_lists[0]

    return ImageSet(**image_set_dict)


def split_image_set(
    image_set: ImageSet,
    by: str = "pol",
    mode: str = "image",
) -> dict[str, list[Path]]:
    """Split an ImageSet by a given field.

    Args:
        image_set (ImageSet): The ImageSet to split
        by (str, optional): Field to split images by. Defaults to "pol".
        mode (str, optional): Type of images to extract from ImageSet. Defaults to "image".

    Raises:
        NamingException: If the field to split by is not found in the ImageSet

    Returns:
        dict[str, list[Path]]: _description_
    """
    logger.info(f"Splitting {image_set=} by {by=} with {mode=}")
    try:
        image_list = getattr(image_set, mode)
    except AttributeError as e:
        msg = f"Failed to extract {mode=} from {image_set=}"
        raise NamingException(msg) from e
    split_list = split_images(images=image_list, by=by)

    return split_list


def split_and_get_image_set(
    image_set: ImageSet,
    get: str,
    by: str = "pol",
    mode: str = "image",
) -> list[Path]:
    """Split an ImageSet by a given field and return the images that match the field of interest.

    Args:
        image_set (ImageSet): The ImageSet to split
        get (str): The field to extract from the split images
        by (str, optional): The field to split by. Defaults to "pol".
        mode (str, optional): The mode to extract from ImageSet. Defaults to "image".

    Raises:
        NamingException: If the field to split by is not found in the ImageSet

    Returns:
        list[Path]: The images that match the field of interest
    """
    split_dict = split_image_set(image_set=image_set, by=by, mode=mode)

    split_list = split_dict.get(get, None)
    if split_list is None:
        raise NamingException(f"Failed to get {get=} from {split_dict=}")

    return split_list


def transpose_and_sort_channel_images(
    beam_channel_images: list[list[Path]],
) -> list[list[Path]]:
    """Regroup per-beam lists of sub-band channel images into per-channel lists
    of beam images, ready to be co-added one channel at a time.

    Each beam's images are sorted by their channel range, and every beam must
    contribute the same number of channels.

    Args:
        beam_channel_images (list[list[Path]]): For each beam, the list of its per-channel images.

    Returns:
        list[list[Path]]: For each channel, the list of beam images at that channel.
    """

    def _channel_key(path: Path) -> int:
        processed_name = extract_components_from_name(path)
        if not isinstance(processed_name, ProcessedNameComponents):
            msg = f"Expected Flint-named images e.g. ProcessedNameComponents. Got {type(processed_name)}"
            raise ValueError(msg)
        channel_range = processed_name.channel_range
        assert channel_range is not None, f"No channel range in {path=}"
        return channel_range[0]

    sorted_beams = [sorted(images, key=_channel_key) for images in beam_channel_images]
    channel_counts = {len(images) for images in sorted_beams}
    assert len(channel_counts) == 1, (
        f"Beams contribute differing channel counts: {channel_counts}"
    )

    return [list(channel_group) for channel_group in zip(*sorted_beams)]


def combine_image_set_to_cube(
    image_set: ImageSet,
    fitscube_options: FitsCubeOptions,
) -> ImageSet:
    """Combine wsclean subband channel images into a cube. Each collection attribute
    of the input `image_set` will be inspected. The MFS images will be ignored.

    A output file name will be generated based on the  prefix and mode (e.g. `image`, `residual`, `psf`, `dirty`).

    Args:
        image_set (ImageSet): Collection of wsclean image productds
        fitscube_options (FitsCubeOptions): Options to control the cube creation, forwarded to ``combine_images_to_cube`` for each mode

    Returns:
        ImageSet: Updated iamgeset describing the new outputs
    """
    logger.info("Combining subband image products into fits cubes")

    if not isinstance(image_set, ImageSet):
        raise TypeError(
            f"Input image_set of type {type(image_set)}, expect {type(ImageSet)}"
        )

    image_set_dict = options_to_dict(input_options=image_set)

    for mode in ("image", "residual", "dirty", "model", "psf"):
        if image_set_dict[mode] is None or not isinstance(
            image_set_dict[mode], (list, tuple)
        ):
            logger.info(f"{mode=} is None or not appropriately formed. Skipping. ")
            continue

        subband_images = [
            image for image in image_set_dict[mode] if ".MFS." not in image.name
        ]
        if len(subband_images) <= 1:
            logger.info(f"Not enough subband images for {mode=}, not creating a cube")
            continue

        output_cube_name = combine_images_to_cube(
            images=subband_images,
            prefix=str(image_set.prefix),
            mode=mode,
            fitscube_options=fitscube_options,
        )

        image_set_dict[mode] = [output_cube_name] + [
            image for image in image_set_dict[mode] if image not in subband_images
        ]

    return ImageSet(**image_set_dict)


def write_stokes_i(xx_path: Path, yy_path: Path, i_path: Path) -> Path:
    """Write the Stokes I image (XX + YY) / 2 of a pair of XX and YY images. The
    XX header is used, with its STOKES axis set to I.

    Args:
        xx_path (Path): The XX image
        yy_path (Path): The YY image, on the same pixel grid as ``xx_path``
        i_path (Path): Where the Stokes I image is written. Overwritten if it exists.

    Raises:
        ValueError: The XX and YY images have different restoring beams

    Returns:
        Path: The Stokes I image
    """
    xx_header = fits.getheader(xx_path)
    yy_header = fits.getheader(yy_path)
    for key in ("BMAJ", "BMIN", "BPA"):
        if key not in xx_header or key not in yy_header:
            continue
        # wsclean marks a blank plane with a zero beam, but a pirate trusts no
        # header. Should NaNs come aboard both, they be the same beam, matey
        if not np.isclose(xx_header[key], yy_header[key], equal_nan=True):
            msg = (
                f"{xx_path.name} and {yy_path.name} have different {key}, "
                f"{xx_header[key]} and {yy_header[key]}"
            )
            raise ValueError(msg)

    header = xx_header.copy()
    for axis in range(1, header["NAXIS"] + 1):
        if header.get(f"CTYPE{axis}") == "STOKES":
            header[f"CRVAL{axis}"] = 1.0
    header["HISTORY"] = (
        f"Stokes I is (XX + YY) / 2 of {xx_path.name} and {yy_path.name}"
    )

    data = (fits.getdata(xx_path) + fits.getdata(yy_path)) / 2
    fits.writeto(i_path, data=data, header=header, overwrite=True)

    return i_path


def form_stokes_i(image_set: ImageSet, keep_linear: bool = False) -> ImageSet:
    """Replace the XX and YY images of an image set by Stokes I, I = (XX + YY) / 2.

    Each XX image is paired with the YY image whose name differs only in its
    ``xx`` field, and the Stokes I image is named with ``i`` there instead. The
    ``xxyy`` field of the prefix likewise becomes ``i``, as it does in the name of
    the PSF, which XX and YY share and so is the Stokes I PSF too. The source list
    is carried over unchanged.

    Args:
        image_set (ImageSet): The XX and YY images, named by flint
        keep_linear (bool, optional): Keep the XX and YY images rather than deleting them. Defaults to False.

    Raises:
        NamingException: The images are not all in XX and YY pairs

    Returns:
        ImageSet: The Stokes I images
    """
    stokes_i: dict[str, list[Path]] = {}
    linear_images: list[Path] = []
    for mode in ("image", "residual", "model", "dirty"):
        images = getattr(image_set, mode)
        if not images:
            continue

        pairs = [
            (image, replace_name_field(path=image, old="xx", new="yy"))
            for image in images
            if "xx" in image.name.split(".")
        ]
        paired = {path for pair in pairs for path in pair}
        if paired != set(images):
            unpaired = sorted(str(path) for path in paired ^ set(images))
            msg = f"The {mode} images are not all XX and YY pairs: {unpaired}"
            raise NamingException(msg)

        logger.info(f"Forming {len(pairs)} Stokes I {mode} images from XX and YY")
        stokes_i[mode] = [
            write_stokes_i(
                xx_path=xx_path,
                yy_path=yy_path,
                i_path=replace_name_field(path=xx_path, old="xx", new="i"),
            )
            for xx_path, yy_path in pairs
        ]
        linear_images += images

    if not keep_linear:
        logger.info(f"Removing {len(linear_images)} XX and YY images")
        remove_files_folders(*linear_images)

    linear_token = pol_name_token(pol="xx,yy")
    if image_set.psf:
        stokes_i["psf"] = [
            psf.rename(replace_name_field(path=psf, old=linear_token, new="i"))
            for psf in image_set.psf
        ]

    prefix = replace_name_field(path=Path(image_set.prefix), old=linear_token, new="i")
    return image_set.with_options(prefix=str(prefix), **stokes_i)
