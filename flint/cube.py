"""Create, split and adjust FITS cubes"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from astropy.io import fits
from fitscube.bounding_box import BoundingBox
from fitscube.combine_fits import combine_fits, compress_cube
from fitscube.exceptions import ShapeMismatchException, TargetAxisMissingException
from fitscube.extract import (
    ExtractPlanesOptions,
    count_cube_planes,
    extract_planes_from_cube,
    find_target_axis,
)

from flint.exceptions import NamingException, NotSupportedError, ShapeMismatchError
from flint.logging import logger
from flint.naming import (
    create_image_cube_name,
    create_path_from_processed_name_components,
    processed_ms_format,
)
from flint.options import FitsCubeOptions
from flint.utils import remove_files_folders

if TYPE_CHECKING:
    from flint.convol import BeamShape


def combine_images_to_cube(
    images: list[Path],
    prefix: str,
    mode: str,
    fitscube_options: FitsCubeOptions,
    bounding_box: bool | BoundingBox | None = None,
    beam_shape: BeamShape | None = None,
) -> Path:
    """Combine wsclean subband channel images into a cube. Each collection attribute
    of the input `image_set` will be inspected. The MFS images will be ignored.

    A output file name will be generated based on the  prefix and mode (e.g. `image`, `residual`, `psf`, `dirty`).

    Args:
        images (list[Path]): The images to combine into a cube
        prefix (str): The prefix of the images to combine
        mode (str): The type of images to combine, e.g. `image`, `residual`, `psf`, `dirty`
        fitscube_options (FitsCubeOptions): Options to control the cube creation
        bounding_box (bool | BoundingBox | None, optional): Overrides ``fitscube_options.bounding_box``
        when given. Used to force a box shared with another cube (e.g. weights) rather than
        letting fitscube compute one for this cube alone. Defaults to None.
        beam_shape (BeamShape | None, optional): The one beam every plane was convolved to. When given the
        cube records it in its header and carries no beam table. Defaults to None.

    Returns:
        Path: The path to the created FITS cube
    """
    logger.info("Combining subband images into fits cubes")
    logger.info(f"Running on {socket.gethostname()=}")

    output_cube_name = create_image_cube_name(image_prefix=Path(prefix), mode=mode)

    logger.info(f"Combining {len(images)} images into {output_cube_name}")
    logger.debug(f"{images=}")
    start = time.perf_counter()
    # fitscube checks the pixel grids itself, in parallel, before it writes
    # anything, so there is no need to walk every header again first
    try:
        freqs = combine_fits(
            file_list=images,
            out_cube=output_cube_name,
            max_workers=fitscube_options.max_workers,
            invalidate_zeros=fitscube_options.invalidate_zeros,
            bounding_box=fitscube_options.bounding_box
            if bounding_box is None
            else bounding_box,
            create_blanks=fitscube_options.create_blanks,
            overwrite=fitscube_options.overwrite,
        )
    except ShapeMismatchException as e:
        msg = f"Images to be cubed have differing pixel grids: {e}"
        raise ShapeMismatchError(msg) from e
    logger.info(
        f"Combined {len(images)} images into {output_cube_name.name} in "
        f"{time.perf_counter() - start:.0f}s"
    )

    rotate_cube(output_cube_name, inplace=fitscube_options.inplace)
    if beam_shape is not None:
        set_cube_beam(cube=output_cube_name, beam_shape=beam_shape)

    output_freqs_name = output_cube_name.with_suffix(".freqs_Hz.txt")
    np.savetxt(output_freqs_name, freqs.to("Hz").value)

    if fitscube_options.remove_original_images:
        remove_files_folders(*images)

    if fitscube_options.compress:
        start = time.perf_counter()
        output_cube_name = compress_cube(
            output_cube_name,
            method=fitscube_options.compress_method,
            max_workers=fitscube_options.max_workers,
            quantize_level=fitscube_options.compress_quantize_level,
        )
        logger.info(
            f"Compressed to {output_cube_name.name} in {time.perf_counter() - start:.0f}s"
        )

    return output_cube_name


def split_cube_into_planes(
    cube: Path, output_path: Path | None = None, num_workers: int = 4
) -> list[Path]:
    """Extract each channel of a FITS cube into its own image, named following the
    flint processed name format so that the planes may be regrouped across beams
    by ``transpose_and_sort_channel_images``.

    Args:
        cube (Path): The FITS cube to split apart
        output_path (Path | None, optional): Directory the planes are written into. Only the flint name fields are retained, so cubes that share them (e.g. an image cube and its weights) need separate directories. Defaults to alongside ``cube``.
        num_workers (int, optional): Planes written at once. Defaults to 4.

    Returns:
        list[Path]: The per-channel images extracted from ``cube``

    Raises:
        NotSupportedError: If ``cube`` is compressed (.gz or .fz), as astropy cannot
            memmap a compressed FITS file and would decompress the whole cube
            into memory. Set ``FitsCubeOptions.compress=False`` for cubes that
            get split.
    """
    if cube.suffix in (".gz", ".fz"):
        msg = (
            f"{cube=} is compressed and cannot be split into planes: "
            "astropy cannot memmap a compressed FITS file, so the per-channel "
            "reads would decompress the whole cube into memory. "
            "Disable FitsCubeOptions.compress for cubes that will be split, or "
            "decompress it first with `fitscube decompress`."
        )
        raise NotSupportedError(msg)

    components = processed_ms_format(in_name=cube)
    if components is None:
        msg = f"Expected a flint named cube. Got {cube=}"
        raise NamingException(msg)

    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)

    def _plane_path(channel: int) -> Path:
        # Only the flint name fields are retained, so a single cube per beam
        # should be split at a time to avoid clobbering planes
        plane_base = create_path_from_processed_name_components(
            processed_name_components=components._replace(
                channel_range=(channel, channel)
            ),
            parent_path=cube.parent if output_path is None else output_path,
        )
        return Path(f"{plane_base}.fits")

    channels = count_cube_planes(header=fits.getheader(cube))
    logger.info(f"Splitting {cube} into {channels} planes")

    return extract_planes_from_cube(
        cube,
        ExtractPlanesOptions(
            output_paths=[_plane_path(channel) for channel in range(channels)],
            overwrite=True,
            max_workers=num_workers,
        ),
    )


def set_cube_beam(cube: Path, beam_shape: BeamShape) -> Path:
    """Write ``beam_shape`` into the cube's header and drop any beam table.

    fitscube writes a table whenever the planes' beams differ, and the blank
    planes of a cube convolved to one beam carry a zero beam that counts as a
    difference. CARTA can not open the table that results.

    Args:
        cube (Path): The cube to update in place
        beam_shape (BeamShape): The beam every populated plane is at

    Raises:
        ValueError: ``beam_shape`` is not finite, as when no image had a beam to solve one from

    Returns:
        Path: The updated cube
    """
    beam_values = (beam_shape.bmaj_arcsec, beam_shape.bmin_arcsec, beam_shape.bpa_deg)
    if not np.all(np.isfinite(beam_values)):
        msg = (
            f"Can not set the beam of {cube.name} to {beam_shape}, as it is not "
            "finite. A common beam is NaN when no image had a usable beam to solve "
            "it from, e.g. every channel is beyond the beam cutoff. Raise the "
            "cutoff or set a fixed beam shape."
        )
        raise ValueError(msg)

    with fits.open(cube) as open_fits:
        header = open_fits[0].header.copy()
        file_info = open_fits.fileinfo(0)
    assert file_info is not None, f"Can not locate the primary HDU of {cube}"
    header_size = file_info["datLoc"] - file_info["hdrLoc"]
    data_end = file_info["datLoc"] + file_info["datSpan"]

    header.remove("CASAMBM", ignore_missing=True)
    fitscube_comments = ("The PSF in each", "Full beam information", "The value '")
    for idx in reversed(range(len(header))):
        card = header.cards[idx]
        if card.keyword == "COMMENT" and str(card.value).startswith(fitscube_comments):
            del header[idx]
    header["BMAJ"] = beam_shape.bmaj_arcsec / 3600
    header["BMIN"] = beam_shape.bmin_arcsec / 3600
    header["BPA"] = beam_shape.bpa_deg
    while len(header.tostring()) < header_size:
        header.append(fits.Card(), useblanks=False, bottom=True)

    if len(header.tostring()) > header_size:
        logger.info(f"New header outgrows the old in {cube.name}, rewriting the file")
        with fits.open(cube, mode="update") as open_fits:
            del open_fits[1:]
            open_fits[0].header = header
        return cube

    # Only the header changes and the table sits after the data, so the cube's
    # data is left where it is rather than rewriting the whole file
    logger.info(f"Setting the beam of {cube.name} to {beam_shape}")
    with cube.open("r+b") as file_handle:
        file_handle.seek(file_info["hdrLoc"])
        file_handle.write(header.tostring().encode("ascii"))
        file_handle.truncate(data_end)

    return cube


def rotate_cube(output_cube_path: str | Path, inplace: bool = True) -> Path:
    """Rotate the FITS cube axes to a shape of (chan, pol, dec, ra)
    # which is what yandasoft linmos tasks expect.

    Args:
        output_cube_path (str | Path): Path to the FITS cube to rotate.
        inplace (bool, optional): If True, modify the file in-place. If False, write to a temporary file and then replace the original. Defaults to True.

    Returns:
        Path: Path to the rotated FITS cube.
    """
    output_path = Path(output_cube_path)
    logger.info(f"Rotating FITS axes of {output_path.name}")

    if not Path(output_cube_path).exists():
        logger.warning(f"{output_cube_path=} does not appear to exist. Returning.")
        return Path(output_cube_path)

    header = fits.getheader(output_path)

    # Cubes formed from planes that were themselves cut from a rotated cube are
    # already in the desired order, and rotating again would undo it
    try:
        if find_target_axis(header=header).axis == header["NAXIS"]:
            logger.info(f"{output_path.name} is already (chan, pol, dec, ra)")
            return output_path
    except TargetAxisMissingException:
        logger.warning(f"No frequency axis in {output_path.name}, not rotating")
        return output_path

    # Swap axes in header
    tmp_header = header.copy()
    for a, b in ((3, 4), (4, 3)):
        # Can not rotate if the keys do not exist. This
        # can happen if uneven plans in cube
        if f"CTYPE{b}" not in tmp_header:
            continue

        for key in ("NAXIS", "CTYPE", "CRPIX", "CRVAL", "CDELT", "CUNIT"):
            # A key absent on the source axis (e.g. CUNIT on STOKES) has to be
            # removed from the destination, else a stale value is left behind
            if f"{key}{b}" in tmp_header:
                header[f"{key}{a}"] = tmp_header[f"{key}{b}"]
            else:
                header.pop(f"{key}{a}", None)

    # Swapping a length-one axis leaves the byte order on disk unchanged, so the
    # moveaxis is a contiguous view of the memmap and nothing is read into memory.
    # Reading a whole cube in is enough to blow a worker's memory budget
    if header["NAXIS"] == 4 and min(header["NAXIS3"], header["NAXIS4"]) == 1:
        logger.info(f"Rotating {output_path.name} via a header rewrite only")
        original_header = fits.getheader(output_path)
        original_header_string = original_header.tostring()
        new_header_string = header.tostring()
        logger.info(
            f"Old header length {len(original_header_string)} bytes, new header length {len(new_header_string)} bytes"
        )
        assert len(original_header_string) == len(new_header_string), (
            f"Header length mismatch: {len(original_header_string)} != {len(new_header_string)}. "
            "This should not happen, and indicates a bug in the header rotation code."
        )
        logger.info("Writing new header to FITS cube in place")
        with open(output_path, mode="r+b") as f:
            f.write(new_header_string.encode("ascii"))

        return output_path

    # Move data axis: (pol, chan, dec, ra) → (chan, pol, dec, ra)
    with fits.open(output_path, mode="readonly", memmap=True) as hdul:
        data_cube = hdul[0].data.copy()
    rotated_data = np.moveaxis(data_cube, 1, 0)

    if inplace:
        logger.info(f"Rotating {output_path.name} in place")
        with fits.open(output_path, mode="update", memmap=True) as hdul:
            hdul[0].data = rotated_data
            hdul[0].header = header
            hdul.flush()
        return output_path

    # Not in-place: write to temp then replace
    tmp_path = output_path.with_name(f"{output_path.stem}_rotated{output_path.suffix}")
    logger.info(f"Writing rotated cube to temporary file {tmp_path.name}")
    fits.PrimaryHDU(data=rotated_data, header=header).writeto(tmp_path, overwrite=True)

    output_path.unlink()
    tmp_path.rename(output_path)
    logger.info(f"Replaced original {output_path.name} with rotated cube")
    return output_path
