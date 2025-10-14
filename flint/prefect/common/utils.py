"""Common prefect related utilities that can be used between flows."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Iterable, ParamSpec, TypeVar
from uuid import UUID

import astropy.units as u
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from prefect import task
from prefect.artifacts import create_markdown_artifact

from flint.archive import copy_sbid_files_archive, create_sbid_tar_archive
from flint.logging import logger
from flint.naming import (
    add_timestamp_to_path,
    get_fits_cube_from_paths,
    get_sbid_from_path,
    rename_linear_to_stokes,
)
from flint.options import ArchiveOptions
from flint.summary import (
    create_beam_summary,
    create_field_summary,
    update_field_summary,
)

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")
C = TypeVar("C", bound=Callable)

SUPPORTED_IMAGE_TYPES = ("png",)


@task
def task_create_object(object: C, **kwargs) -> C:
    return object(**kwargs)


@task
def task_getattr(
    item: object,
    attribute: str,
    /,
) -> Any:
    """Retrieve an attribute from an input instance of a class or structure.

    Args:
        item (Any): The item that has the input class or structure
        attribute (str): The attribute to extract

    Returns:
        Any: Value of the requested attribute
    """
    logger.debug(f"Pulling {attribute=}")
    return getattr(item, attribute)


@task
def task_sorted(
    iterable: Iterable[T],
    /,
    *,
    key: Any = None,
    reverse: bool = False,
) -> list[T]:
    return sorted(iterable, key=key, reverse=reverse)


@task
def task_zip_list_of_list(list_of_list: list[list[T]]) -> list[tuple[T, ...]]:
    return list(zip(*list_of_list))


def upload_image_as_artifact(
    image_path: Path, description: str | None = None
) -> UUID | None:
    """Create and submit a markdown artifact tracked by prefect for an
    input image. Currently supporting png formatted images.

    The input image is converted to a base64 encoding, and embedded directly
    within the markdown string. Therefore, be mindful of the image size as this
    is tracked in the postgres database.

    Args:
        image_path (Path): Path to the image to upload
        description (Optional[str], optional): A description passed to the markdown artifact. Defaults to None.

    Returns:
        UUID | None: Generated UUID of the registered artifact. If the upload fails ``None`` is returned.
    """
    from prefect.exceptions import PrefectHTTPStatusError

    image_type = image_path.suffix.replace(".", "")
    assert image_path.exists(), f"{image_path} does not exist"
    assert image_type in SUPPORTED_IMAGE_TYPES, (
        f"{image_path} has type {image_type}, and is not supported. Supported types are {SUPPORTED_IMAGE_TYPES}"
    )

    with open(image_path, "rb") as open_image:
        logger.info(f"Encoding {image_path} in base64")
        image_base64 = base64.b64encode(open_image.read()).decode()

    logger.info("Creating markdown tag")
    markdown = f"![{image_path.stem}](data:image/{image_type};base64,{image_base64})"

    try:
        image_uuid: UUID | None = None
        logger.info("Registering artifact")
        image_uuid = create_markdown_artifact(
            markdown=markdown, description=description
        )
    except PrefectHTTPStatusError as e:
        logger.warning("Failed to register artefact. ")
        logger.warning(f"{e=}")

    return image_uuid


task_update_field_summary = task(update_field_summary)
task_create_field_summary = task(create_field_summary)
task_create_beam_summary = task(create_beam_summary)
task_get_fits_cube_from_paths = task(get_fits_cube_from_paths)
task_rename_linear_to_stokes = task(rename_linear_to_stokes)


@task
def task_archive_sbid(
    science_folder_path: Path,
    archive_path: Path | None = None,
    copy_path: Path | None = None,
    max_round: int | None = None,
    update_archive_options: dict[str, Any] | None = None,
) -> Path:
    """Create a tarball of files, or copy files, from a processing folder.

    Args:
        science_folder_path (Path): Path that contains the imaged produced
        archive_path (Optional[Path], optional): Location to create and store the tar ball at. If None no tarball is created. Defaults to None.
        copy_path (Optional[Path], optional): Location to copy selected files into. If None no files are copied. Defaults to None.
        max_round (Optional[int], optional): The last self-calibration round performed. If provided some files form this round are copied (assuming wsclean imaging). If None, the default file patterns in ArchiveOptions are used. Defaults to None.
        update_archive_options (Optional[Dict[str, Any]], optional): Additional options to provide to ArchiveOptions. Defaults to None.

    Returns:
        Path: The science folder files were copied from
    """

    sbid = get_sbid_from_path(path=science_folder_path)

    archive_options = ArchiveOptions()

    if update_archive_options:
        logger.info(f"Updating archive options with {update_archive_options=}")
        archive_options = archive_options.with_options(**update_archive_options)

    # TODO: What should this be? Just general new regexs passed through,
    # or is this fine?
    if max_round:
        updated_file_patterns = (
            *tuple(archive_options.tar_file_re_patterns),
            f".*beam[0-9]+\\.round{max_round}-.*-image\\.fits",
            f".*beam[0-9]+\\.round{max_round}\\.ms\\.(zip|tar)",
        )
        archive_options = archive_options.with_options(
            tar_file_re_patterns=updated_file_patterns
        )

    if archive_path:
        tar_file_name = add_timestamp_to_path(Path(archive_path) / f"SB{sbid}.tar")
        create_sbid_tar_archive(
            tar_out_path=tar_file_name,
            base_path=science_folder_path,
            archive_options=archive_options,
        )

    if copy_path:
        copy_sbid_files_archive(
            copy_out_path=copy_path,
            base_path=science_folder_path,
            archive_options=archive_options,
        )

    return science_folder_path


@task
def task_update_with_options(input_object: T, **kwargs) -> T:
    """Updated any object via its `.with_options()` interface.

    All key-word arguments other than `input_object` are passed through
    to that `input_object`s `.with_options()` method.

    Args:
        input_object (T): The object that has an `.with_options` method that will be updated

    Returns:
        T: The updated object
    """
    assert "with_options" in dir(input_object), (
        f"{type(input_object)=} does not have a with_options method"
    )
    updated_object = input_object.with_options(**kwargs)  # type: ignore

    return updated_object


@task
def task_get_attributes(item: Any, attribute: str) -> Any:
    """Retrieve an attribute from an input instance of a class or structure.

    This is intended to be used when dealing with a prefect future object that
    has yet to be evaluated or is otherwise not immediately accessible.

    Args:
        item (Any): The item that has the input class or structure
        attribute (str): The attribute to extract

    Returns:
        Any: Value of the requested attribute
    """
    logger.debug(f"Pulling {attribute=}")
    return item.__dict__[attribute]


@task
def task_flatten(to_flatten: list[list[T]]) -> list[T]:
    """Will flatten a list of lists into a single list. This
    is useful for when a task-descorated function returns a list.


    Args:
        to_flatten (List[List[T]]): Input list of lists to flatten

    Returns:
        List[T]: Flattened form of input
    """
    logger.debug(f"Received {len(to_flatten)} to flatten.")

    flatten_list = [item for sublist in to_flatten for item in sublist]

    logger.debug(f"Flattened list {len(flatten_list)}")

    return flatten_list


def extract_frequency_axis_hz(path: Path) -> np.ndarray:
    """
    Return the spectral axis values as frequency in Hz from a FITS image/cube.

    Assumptions:
    - One axis is frequency (CTYPE* contains 'FREQ').
    - Radio convention is assumed (no velocity/wavelength conversions needed).

    Parameters
    ----------
    path : Path
        Path to the FITS file.

    Returns
    -------
    np.ndarray
        1D array of frequencies in Hz, length equals the number of spectral planes.

    Raises
    ------
    ValueError
        If no image HDU with data exists or no frequency axis is found.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"FITS file not found: {path}")

    with fits.open(path, memmap=True) as hdul:
        # pick the first image-like HDU with data
        hdu = next(
            (h for h in hdul if isinstance(h, (fits.PrimaryHDU, fits.ImageHDU)) and h.data is not None),
            None
        )
        if hdu is None:
            raise ValueError("No image HDU with data found in FITS file.")

        header = hdu.header
        naxis = header.get("NAXIS", 0)
        if naxis < 1:
            raise ValueError("FITS header has NAXIS < 1.")

        # locate spectral (frequency) axis by CTYPE*
        spec_axis = None  # 1-based FITS axis index
        for i in range(1, naxis + 1):
            ctype = (header.get(f"CTYPE{i}", "") or "").upper()
            if "FREQ" in ctype:
                spec_axis = i
                break
        if spec_axis is None:
            raise ValueError("No frequency spectral axis found (CTYPE* does not contain 'FREQ').")

        n_spec = header.get(f"NAXIS{spec_axis}", 0)
        if n_spec <= 0:
            raise ValueError(f"Invalid frequency axis length: NAXIS{spec_axis}={n_spec}")

        # Build WCS (not using .celestial since we are not doing RA/DEC transforms)
        w = WCS(header)

        # Prepare pixel coordinates: vary spectral axis 1..NAXIS_spec, others fixed at CRPIX
        pix_ref = [float(header.get(f"CRPIX{i}", 1.0)) for i in range(1, naxis + 1)]
        coords = np.tile(pix_ref, (n_spec, 1))
        coords[:, spec_axis - 1] = np.arange(1.0, n_spec + 1.0)  # FITS is 1-based

        # Pixel -> world
        world = w.wcs_pix2world(coords, 1)
        freq_vals = world[:, spec_axis - 1]

        # Units: default to Hz if CUNIT is missing
        cunit = header.get(f"CUNIT{spec_axis}", "") or "Hz"
        freq = (freq_vals * u.Unit(cunit)).to(u.Hz).value

        if not np.all(np.isfinite(freq)):
            raise ValueError("Non-finite values encountered when computing frequency axis.")
        if np.any(freq <= 0):
            # Not strictly an error in all contexts, but usually indicates a header or unit problem.
            raise ValueError("Non-positive frequency values encountered; check header CUNIT and WCS.")

        return freq

def _scalar_frequency_from_header_hz(header: fits.Header) -> float:
    """
    Fallback: extract a single frequency (Hz) from header when there is no spectral axis.
    Tries common patterns seen in WSclean/CASA outputs.
    """
    # 1) Look for any CTYPEn that declares FREQ alongside a CRVALn (even if NAXIS < n in some edge headers)
    for i in range(1, 8):  # be generous about axes
        ctype = (header.get(f"CTYPE{i}", "") or "").upper()
        if "FREQ" in ctype:
            cunit = header.get(f"CUNIT{i}", "") or "Hz"
            val = header.get(f"CRVAL{i}", None)
            if val is not None:
                return (val * u.Unit(cunit)).to_value(u.Hz)

    # 2) Common scalar frequency keywords sometimes present on per-channel images
    for key in ("FREQ", "CHAN_FREQ", "FREQUENCY", "NU"):
        if key in header:
            return (header[key] * u.Hz).to_value(u.Hz)

    # If we get here, we do not have a reliable scalar frequency
    raise ValueError("Could not find a scalar frequency in header (no FREQ/CHAN_FREQ nor CRVALn with CTYPEn='FREQ').")


def frequencies_from_wsclean_images_hz(paths: Iterable[Path]) -> np.ndarray:
    """
    Given a list of WSclean per-channel images (each file has 1 spectral plane),
    return a numpy array of their frequencies in Hz, preserving input order.

    Strategy per file:
    - Try WCS-based extraction; if it yields exactly one value, use it.
    - Otherwise, fall back to scalar frequency from header via common keywords.

    Parameters
    ----------
    paths : Iterable[Path]
        Iterable of FITS file paths (e.g., Path objects).

    Returns
    -------
    np.ndarray
        Array of shape (N,) with frequencies in Hz corresponding to the input files.

    Raises
    ------
    ValueError
        If any file lacks a determinable frequency.
    """
    freqs: list[float] = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            raise FileNotFoundError(f"FITS file not found: {p}")

        # First attempt: WCS spectral axis
        try:
            fa = extract_frequency_axis_hz(p)
            if fa.size == 1:
                freqs.append(float(fa[0]))
                continue
            # If more than one plane, this is unexpected for per-channel images
            raise ValueError(f"{p} has {fa.size} spectral planes; expected 1.")
        except Exception:
            # Fallback: inspect header for scalar frequency
            with fits.open(p, memmap=True) as hdul:
                hdu = next(
                    (h for h in hdul if isinstance(h, (fits.PrimaryHDU, fits.ImageHDU)) and h.data is not None),
                    None
                )
                if hdu is None:
                    raise ValueError(f"No image HDU with data in {p}.")
                freq_hz = _scalar_frequency_from_header_hz(hdu.header)
                if not np.isfinite(freq_hz) or freq_hz <= 0:
                    raise ValueError(f"Invalid frequency from header in {p}: {freq_hz}")
                freqs.append(float(freq_hz))

    return np.asarray(freqs, dtype=float)