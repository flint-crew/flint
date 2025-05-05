"""Simple interface into wsclean

Some notes around the file naming.

A certain filenaming scheme is required for FITS files to perform leakage correction
when co-adding them together in the yandasoft linmos application. The stokes field
needs to be encoded as ``.i.``. For example:

    >>> `SB1234.RACS_0123+43.beam01.i.image.fits`

The wsclean formatted output string appends something denoted with ``-``. Within
this code there is and attempt to rename the wsclean outputs to replace the ``-``
with a ``.``.

"""

from __future__ import annotations

import re
from argparse import ArgumentParser
from glob import glob
from numbers import Number
from pathlib import Path
from typing import Any, Collection, NamedTuple

import numpy as np
from astropy.io import fits
from fitscube.combine_fits import combine_fits

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
    NamingException,
    NotSupportedError,
)
from flint.logging import logger
from flint.naming import (
    create_image_cube_name,
    create_imaging_name_prefix,
    split_images,
)
from flint.options import (
    MS,
    BaseOptions,
    add_options_to_parser,
    create_options_from_parser,
    options_to_dict,
)
from flint.sclient import run_singularity_command
from flint.utils import (
    hold_then_move_into,
    parse_environment_variables,
    remove_files_folders,
)


class ImageSet(BaseOptions):
    """A structure to represent the images and auxiliary products produced by
    wsclean"""

    prefix: str
    """Prefix of the images and other output products. This should correspond to the -name argument from wsclean"""
    image: list[Path]
    """Images produced. """
    psf: list[Path] | None = None
    """References to the PSFs produced by wsclean. """
    dirty: list[Path] | None = None
    """Dirty images. """
    model: list[Path] | None = None
    """Model images.  """
    residual: list[Path] | None = None
    """Residual images."""
    source_list: Path | None = None
    """Path to a source list that accompanies the image data"""


class WSCleanOptions(BaseOptions):
    """A basic container to handle WSClean options. These attributes should
    conform to the same option name in the calling signature of wsclean

    Basic support for environment variables is available. Should a value start
    with `$` it is assumed to be a environment variable, it is will be looked up.
    Some basic attempts to determine if it is a path is made.

    Should the `temp_dir` options be specified then all images will be
    created in this location, and then moved over to the same parent directory
    as the imaged MS. This is done by setting the wsclean `-name` argument.
    """

    abs_mem: int = 100
    """Memory wsclean should try to limit itself to"""
    local_rms_window: int | None = None
    """Size of the window used to estimate rms noise"""
    size: int = 10128
    """Image size, only a single dimension is required. Note that this means images will be squares. """
    local_rms: bool = True
    """Whether a local rms map is computed"""
    force_mask_rounds: int | None = None
    """Round of force masked derivation"""
    auto_mask: float | None = 3.5
    """How deep the construct clean mask is during each cycle"""
    auto_threshold: float | None = 0.5
    """How deep to clean once initial clean threshold reached"""
    threshold: float | None = None
    """Threshold in Jy to stop cleaning"""
    channels_out: int = 4
    """Number of output channels"""
    mgain: float = 0.7
    """Major cycle gain"""
    nmiter: int = 15
    """Maximum number of major cycles to perform"""
    niter: int = 750000
    """Maximum number of minor cycles"""
    multiscale: bool = False
    """Enable multiscale deconvolution"""
    multiscale_scale_bias: float = 0.75
    """Multiscale bias term"""
    multiscale_gain: float | None = None
    """Size of step made in the subminor loop of multi-scale. Default currently 0.2, but shows sign of instability. A value of 0.1 might be more stable."""
    multiscale_scales: tuple[int, ...] | None = None
    """Scales used for multi-scale deconvolution"""
    fit_spectral_pol: int | None = None
    """Number of spectral terms to include during sub-band subtraction"""
    weight: str = "briggs -0.5"
    """Robustness of the weighting used"""
    data_column: str = "CORRECTED_DATA"
    """Which column in the MS to image"""
    scale: str = "2.5asec"
    """Pixel scale size"""
    gridder: str | None = "wgridder"
    """Use the wgridder kernel in wsclean (instead of the default w-stacking method)"""
    nwlayers: int | None = None
    """Number of w-layers to use if the gridder mode is w-stacking"""
    wgridder_accuracy: float | None = None
    """The accuracy requested of the wgridder (should it be used), compared as the RMS error when compred to a DFT"""
    join_channels: bool = True
    """Collapse the sub-band images down to an MFS image when peak-finding"""
    squared_channel_joining: bool = False
    """Use with -join-channels to perform peak finding in the sum of squared values over
    channels, instead of the normal sum. This is useful for imaging QU polarizations
    with non-zero rotation measures, for which the normal sum is insensitive.
    """
    join_polarizations: bool = False
    """Perform deconvolution by searching for peaks in the sum of squares of the polarizations,
    but subtract components from the individual images. Only possible when imaging two or four Stokes
    or linear parameters. Default: off.
    """
    minuv_l: float | None = None
    """The minimum lambda length that the visibility data needs to meet for it to be selected for imaging"""
    minuvw_m: float | None = None
    """A (u,v) selection command, where any baselines shorter than this will be ignored during imaging"""
    maxw: float | None = None
    """A percentage specifying the maximum w-term to be gridded, relative to the max w-term being considered"""
    no_update_model_required: bool = False
    """Will instruct wsclean not to create the MODEL_DATA column"""
    no_small_inversion: bool = False
    """Disables an optimisation of wsclean's w-gridder mode. This might improve accuracy of the w-gridder. """
    beam_fitting_size: float | None = 1.25
    """Use a fitting box the size of <factor> times the theoretical beam size for fitting a Gaussian to the PSF."""
    fits_mask: Path | None = None
    """Path to a FITS file that encodes a cleaning mask"""
    deconvolution_channels: int | None = None
    """The channels out will be averaged down to this many sub-band images during deconvolution"""
    parallel_deconvolution: int | None = None
    """If not none, then this is the number of sub-regions wsclean will attempt to divide and clean"""
    parallel_gridding: int | None = None
    """If not none, then this is the number of channel images that will be gridded in parallel"""
    temp_dir: str | Path | None = None
    """The path to a temporary directory where files will be written. """
    pol: str = "i"
    """The polarisation to be imaged"""
    save_source_list: bool = False
    """Saves the found clean components as a BBS/DP3 text sky model"""
    channel_range: tuple[int, int] | None = None
    """Image a channel range between a lower (inclusive) and upper (exclusive) bound"""
    no_reorder: bool = False
    """If True turn off the reordering of the MS at the beginning of wsclean"""
    flint_no_log_wsclean_output: bool = False
    """If True do not log the wsclean output"""
    no_mf_weighting: bool = False
    """Opposite of -ms-weighting; can be used to turn off MF weighting in -join-channels mode"""
    flint_make_cube_inplace: bool = True
    """Rotate the cube for the linmos axis ordering in place, or do it via a temporary file that then gets deleted. Good thing to turn off when getting weird OSErrors on file writing"""


class WSCleanResult(BaseOptions):
    """Simple container for a wsclean command."""

    cmd: str
    """The constructede wsclean command that would be executed."""
    options: WSCleanOptions
    """The set of wslean options used for imaging"""
    ms: MS
    """The measurement sets that have been included in the wsclean command. """
    bind_dirs: tuple[Path, ...]
    """Paths that should be binded to when executing the command"""
    move_hold_directories: tuple[Path, Path]
    """Paths that should be moved to and from when executing the command"""
    image_prefix_str: str | None = None
    """The prefix of the images that will be created"""
    cleanup: bool = True
    """Will clean up the dirty images/psfs/residuals/models when the imaging has completed"""
    image_set: ImageSet | None = None
    """The set of images produced by wsclean"""


def image_set_from_result(wsclean_result: WSCleanResult) -> ImageSet | None:
    return wsclean_result.image_set


def combine_images_to_cube(
    images: list[Path],
    prefix: str,
    mode: str,
    remove_original_images: bool = False,
    inplace: bool = True,
) -> Path:
    """Combine wsclean subband channel images into a cube. Each collection attribute
    of the input `image_set` will be inspected. The MFS images will be ignored.

    A output file name will be generated based on the  prefix and mode (e.g. `image`, `residual`, `psf`, `dirty`).

    Args:
        image_set (ImageSet): Collection of wsclean image productds
        remove_original_images (bool, optional): If True, images that went into the cube are removed. Defaults to False.
        inplace (bool, optional): If True, modify the file in-place. If False, write to a temporary file and
        then replace the original. Default True
    Returns:
        ImageSet: Updated iamgeset describing the new outputs
    """
    logger.info("Combining subband images into fits cubes")

    output_cube_name = create_image_cube_name(image_prefix=Path(prefix), mode=mode)

    logger.info(f"Combining {len(images)} images. {images=}")
    freqs = combine_fits(file_list=images, out_cube=output_cube_name)
    rotate_cube(output_cube_name, inplace=inplace)

    # Write out the hdu to preserve the beam table constructed in fitscube
    logger.info(f"Writing {output_cube_name=}")

    output_freqs_name = output_cube_name.with_suffix(".freqs_Hz.txt")
    np.savetxt(output_freqs_name, freqs.to("Hz").value)

    if remove_original_images:
        remove_files_folders(*images)

    return output_cube_name


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

    return ImageSet(**image_set_dict)


def merge_image_sets_from_results(
    wsclean_results: list[WSCleanResult],
) -> ImageSet:
    """Merge the image sets from multiple wsclean results into a single image set.

    Args:
        wsclean_results (List[WSCleanResult]): The results of multiple wsclean imaging runs

    Returns:
        ImageSet: The merged image set
    """
    image_sets = [
        result.image_set for result in wsclean_results if result.image_set is not None
    ]
    return merge_image_sets(image_sets)


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


def get_wsclean_output_source_list_path(
    name_path: str | Path, pol: str | None = None
) -> Path:
    """WSClean can produce a text file that describes the components
    that it cleaned, their type, scale and brightness. These are
    placed in a file that is:

    >> {name}.{pol}-sources.txt

    where ``name`` represented the `-name` component. Given
    an input measurement set path or this `-name` value return
    the expected source list text file. ``pol`` is the stokes
    that the source is expected.

    Args:
        name_path (Union[str,Path]): Value of the ``-name`` option. If `str` converted to a ``Path``
        pol (Optional[str], optional): The polarisation to add to the name. If None the -source.txt suffix is simply appended. Defaults to None.

    Returns:
        Path: Path to the source list text file
    """

    # ye not be trusted
    name_path = Path(name_path)
    base_name = name_path.name
    if ".ms" == Path(base_name).suffix:
        base_name = Path(base_name).stem

    logger.info(f"{base_name=} extracted from {name_path=}")

    source_list_name = (
        f"{base_name}.{pol}-sources.txt" if pol else f"{base_name}-sources.txt"
    )
    source_list_path = name_path.parent / source_list_name

    return source_list_path


def _rename_wsclean_title(name_str: str) -> str:
    """Construct and apply a regular expression that aims to identify
    the wsclean appended properties string within a file and replace
    the `-` separator with a `.`.

    A simple replace of all `-` with `.` may not be ideal if the
    character has been used on purpose.

    Args:
        name_str (str): The name that will be extracted and modified

    Returns:
        str: The modified string if a wsclean string was matched, otherwise the input `name-str`
    """
    # Yolo chatGPT
    # The following should replace the .qu with .q or .u whilst removing the -Q and -U
    logger.info(f"Renaming {name_str=} for qu components if necessary")
    name_str = re.sub(
        r"(\.qu)-([^-]+)-?([QU])?(\-(psf|image|dirty|model|residual)\.fits)",
        lambda m: f".{m.group(3).lower() if m.group(3) else 'q'}-{m.group(2)}{m.group(4)}",
        name_str,
    )

    # Note the negative look back - the (?<!...) bit. This is to make
    # sure that the ch0000-0001 field is not captured. For example:
    # SB57516.RACS_0929-81.beam35.round4.i.ch0287-0288-image.fits
    # would be inadvertently picked up and replaced. The [0-9]{4}
    # we are testing for are the channel indicator when --channel-out is used
    search_re = r"(-(i|q|u|v|xx|xy|yx|yy))?(-(MFS|(<!ch[0-9]{4}-[0-9]{4})[0-9]{4}))?(-t[0-9]{5})?-(image|dirty|model|residual|psf)"
    match_re = re.compile(search_re)

    logger.info(f"Searching {name_str=} for wsclean added filename components")
    result = match_re.search(str(name_str))

    if result is None:
        return name_str

    name = name_str.replace(result[0], result[0].replace("-", "."))

    return name


def _rename_wsclean_file(
    input_path: Path,
    rename_file: bool = False,
) -> Path:
    """Rename a file with wsclean appended string information to convert its
    `-` separation markers with `.`. This should handle skipping the field
    name of the target field observed.

    Args:
        input_path (Path): The file path that would be examined and modified
        clean_parts (Union[int, Tuple[int, ...]], optional): Which parts of a split string will be modified. Defaults to -2.
        rename_file (bool, optional): Whether the file should be moved / renamed. Defaults to False.

    Returns:
        Path: Path to the renamed file
    """
    input_path = Path(input_path)
    file_name = Path(input_path.name)
    new_name = _rename_wsclean_title(name_str=str(file_name))

    new_path = input_path.parent / new_name

    if rename_file:
        logger.info(f"Renaming {input_path} to {new_path}")
        input_path.rename(new_path)

    return new_path


def _wsclean_output_callback(line: str) -> None:
    """Call back function used to detect clean divergence"""

    assert isinstance(line, str)

    if "Iteration" in line and "KJy" in line:
        raise CleanDivergenceError(f"Clean divergence detected: {line}")

    # The second commented out line may be related to rerunning a wsclean task with a
    # existing set of temporary files in the wsclean context. Look into this.
    # TODO: Look at behaviour of wsclean should it be executed in a location where tempoary
    # files from a previous execution exist
    # temp_error_lines = ("Error opening temporary data file", "Input/output error")

    temp_error_lines = ("Input/output error",)
    if any([temp_error_line in line for temp_error_line in temp_error_lines]):
        logger.info(f"Detected input/output error in {line}")
        from time import sleep

        sleep(2)

        raise AttemptRerunException


# TODO: Update this function to also add int the source list
def get_wsclean_output_names(  #
    prefix: str,
    subbands: int | None = None,
    pols: str | tuple[str] | tuple[str, ...] | None = None,
    verify_exists: bool = False,
    include_mfs: bool = True,
    output_types: str | Collection[str] = (
        "image",
        "dirty",
        "residual",
        "model",
        "psf",
    ),
    check_exists_when_adding: bool = False,
) -> ImageSet:
    """Attempt to generate the file names and paths that would be
    created by an imaging run of wsclean. This is done using a the
    known (expected) naming format of modern wsclean versions.

    Checks can be made to ensure a file exists before adding it
    to the output ``ImageSet``. This might be important as some
    wsclean image products might be deleted in order to preserve
    disk space.

    Args:
        prefix (str): The prefix of the imaging run (akin to -name option in wsclean call)
        subbands (int): Number of subbands that were imaged
        pols (Optional[Union[str,Tuple[str]]], optional): The polarisation of the image. If None are provided then this is not used. Multiple polarisation may be supplied. If multiple pols are given in an iterable, each will be produced. Defaults to None.
        verify_exists (bool, optional): Ensures that each generated path corresponds to an actual file. Defaults to False.
        include_mfs (bool, optional): Include the MFS images produced by wsclean. Defaults to True.
        output_types (Union[str,Collection[str]]): Include files of this type, including image, dirty, residual, model, psf. Defaults to  ('image','dirty','residual','model', 'psf').
        check_exists_when_adding (bool, optional): Only add a generated file if it exists. Although related to the ``verify_exists``, when this optional is ``True`` files will silently be ignored if they are not found. if this and ``verify_exists`` are both ``True`` then no errors on missing files will be raised. Defaults to False.

    Raises:
        FileExistsError: Raised when a file does not exist and verify_exists is True.

    Returns:
        ImageSet: The file paths that wsclean should create/has created.
    """
    logger.info(f"Finding wsclean outputs, {prefix=}")
    # TODO: Use a regular expression for this
    subband_strs: list[str | None] = [
        None,
    ]
    if subbands and subbands > 1:
        subband_strs = [f"{subband:04}" for subband in range(subbands)]
        if include_mfs:
            subband_strs.append("MFS")

    in_pols: tuple[None, ...] | tuple[str, ...]
    if pols is None:
        in_pols = (None,)
    elif isinstance(pols, str):
        in_pols = (pols,)
    else:
        in_pols = pols

    if isinstance(output_types, str):
        output_types = (output_types,)

    images: dict[str, list[Path]] = {}
    for image_type in ("image", "dirty", "model", "residual"):
        if image_type not in output_types:
            continue

        paths: list[Path] = []
        for pol in in_pols:
            for subband_str in subband_strs:
                components = [prefix]
                if subband_str:
                    components.append(subband_str)
                if pol:
                    components.append(pol)
                components.append(image_type)
                path_str = "-".join(components) + ".fits"

                if check_exists_when_adding and not Path(path_str).exists():
                    logger.debug(f"{path_str} does not existing. Not adding. ")
                    continue

                paths.append(Path(path_str))

        images[image_type] = paths

    # The PSF is the same for all stokes
    if "psf" in output_types:
        psf_images = [
            (
                Path(f"{prefix}-{subband_str}-psf.fits")
                if subband_str
                else Path(f"{prefix}-psf.fits")
            )
            for subband_str in subband_strs
        ]
        # Filter out files if they do not exists
        if check_exists_when_adding:
            psf_images = [psf_image for psf_image in psf_images if psf_image.exists()]

        images["psf"] = psf_images

    if verify_exists:
        paths_no_exists: list[Path] = []
        for _, check_paths in images.items():
            paths_no_exists += [path for path in check_paths if not path.exists()]
        if len(paths_no_exists) > 0:
            raise FileExistsError(
                f"The following {len(paths_no_exists)} files do not exist: {paths_no_exists}"
            )

    return ImageSet(prefix=prefix, **images)


def delete_wsclean_outputs(
    prefix: str,
    output_type: str = "image",
    ignore_mfs: bool = True,
    no_subbands: bool = False,
) -> list[Path]:
    """Attempt to remove elected wsclean output files

    If ``no_subbands`` is True (as in ``channels_out`` is 1) then nothing is deleted.

    Args:
        prefix (str): The prefix of the files to remove. This would correspond to the -name of wsclean.
        output_type (str, optional): What type of wsclean output to try to remove. Defaults to 'image'.
        ignore_mfs (bool, optional): If True, do not remove MFS outputs (attempt to, at least). Defaults to True.
        no_subbands (bool, Optional): If True, nothing is deleted. Defaults to False.

    Returns:
        List[Path]: The paths that were removed (or at least attempted to be removed)/
    """
    # TODO: This glob needs to be replaced with something more explicit
    paths = [Path(p) for p in glob(f"{prefix}-*{output_type}.fits")]
    logger.info(f"Found {len(paths)} matching {prefix=} and {output_type=}.")
    rm_paths: list[Path] = []

    for path in paths:
        if no_subbands:
            continue
        if ignore_mfs and "-MFS-" in str(path.name):
            logger.info(f"{path=} appears to be an MFS product, not removing. ")
            continue
        if path.exists():
            logger.warning(f"Removing {path}.")
            try:
                rm_paths.append(path)
                path.unlink()
            except Exception as e:
                logger.critical(f"Removing {path} failed: {e}")

    return rm_paths


# TODO: This should be expanded to denote levels of things to delete
# TODO: Need to create a regex based mode, and better handlijng of -MFS-,
# which is only created when -join-channels is used
def wsclean_cleanup_files(
    prefix: str | Path,
    output_types: tuple[str, ...] | None = ("dirty", "psf", "model", "residual"),
    single_channel: bool = False,
) -> tuple[Path, ...]:
    """Clean up (i.e. delete) files from wsclean.

    Args:
        prefix (Union[str, Path]): The prefix used to search for files. This is generally the -name
        output_types (Optional[Tuple[str]], optional): Which type of output wsclean products to delete. Defaults to ("dirty", "psf", "model", "residual").
        single_channel (bool, optional): Whether there is the subband part of the wsclean file names to consider. Defaults to False.

    Returns:
        Tuple[Path, ...]: Set of files that were deleted
    """
    rm_files = []
    logger.info(f"Removing wsclean files with {prefix=} {output_types=}")

    if output_types is not None:
        for output_type in output_types:
            rm_files += delete_wsclean_outputs(
                prefix=str(prefix),
                output_type=output_type,
                no_subbands=single_channel and output_type == "image",
            )

    return tuple(rm_files)


def create_wsclean_name_argument(wsclean_options: WSCleanOptions, ms: MS) -> Path:
    """Create the value that will be provided to wsclean -name argument. This has
    to be generated. Among things to consider is the desired output directory of imaging
    files. This by default will be alongside the measurement set. If a `temp_dir`
    has been specified then output files will be written here.

    Args:
        wsclean_options (WSCleanOptions): Set of wsclean options to consider
        ms (MS): The measurement set to be imaged

    Returns:
        Path: Value of the -name argument to provide to wsclean
    """
    wsclean_options_dict = wsclean_options._asdict()

    # Prepare the name for the output wsclean command
    # Construct the name property of the string
    pol = wsclean_options.pol
    channel_range = wsclean_options.channel_range
    name_prefix_str = create_imaging_name_prefix(
        ms_path=ms.path, pol=pol, channel_range=channel_range
    )

    # Now resolve the directory part
    name_dir: Path | str | None = ms.path.parent
    temp_dir = wsclean_options_dict.get("temp_dir", None)
    if temp_dir:
        # attempt to resolve possible environment variables flexibly
        name_dir = parse_environment_variables(variable=temp_dir)

    assert name_dir is not None, f"{name_dir=} is None, which is bad"

    name_argument_path = Path(name_dir) / name_prefix_str
    logger.info(f"Constructed -name {name_argument_path}")

    return name_argument_path


class ResolvedCLIResult(NamedTuple):
    """Mapping results to provide to wsclean"""

    cmd: str | None = None
    """The argument value pair to place on the CLI. """
    unknown: Any | None = None
    """Unknown options that could not be converted"""
    bindpath: Path | None = None
    """A path to bind to when called within a container"""
    ignore: bool = False
    """Ignore this CLIResult if True"""


def _resolve_wsclean_key_value_to_cli_str(key: str, value: Any) -> ResolvedCLIResult:
    """An internal function intended to map a key-value pair to
    the appropriate form to pass to a CLI call into wsclean.

    Args:
        key (str): The wsclean argument name to consider. Underscores will be converted to hyphens, as expected by wsclean
        value (Any): The value of the argument that should be converted to the appropriately formatted string

    Returns:
        ResolvedCLIResult: Converted CLI output, including paths to bind to and unknown conversions
    """

    # Some wsclean options, if multiple values are provided, might need
    # to be join as a csv list. Others might want to be dumped in. Just
    # attempting to future proof (arguably needlessly).
    options_to_comma_join = "multiscale-scales"
    bind_dir_options = ("temp-dir",)

    logger.debug(f"{key=} {value=} {type(value)=}")

    value = (
        parse_environment_variables(variable=value) if isinstance(value, str) else value
    )

    cmd = None
    unknown = None
    bind_dir_path = None

    original_key = key
    key = key.replace("_", "-")

    if original_key.startswith("flint_"):
        # No need to do anything more
        return ResolvedCLIResult(ignore=True)
    elif key == "size":
        cmd = f"-size {value} {value}"
    elif isinstance(value, bool):
        if value:
            cmd = f"-{key}"
    elif isinstance(value, (str, Number)):
        cmd = f"-{key} {value}"
    elif isinstance(value, (list, tuple)):
        value = list(map(str, value))
        value_str = ",".join(value) if key in options_to_comma_join else " ".join(value)
        cmd = f"-{key} {value_str}"
    elif isinstance(value, Path):
        value_str = str(value)
        cmd = f"-{key} {value_str}"
    elif value is None:
        logger.debug(
            f"{key} option set to {value}. Not sure what this means. Ignoring. "
        )
    else:
        unknown = (original_key, value)

    if key in bind_dir_options and isinstance(value, (str, Path)):
        bind_dir_path = Path(value)

    return ResolvedCLIResult(cmd=cmd, unknown=unknown, bindpath=bind_dir_path)


def create_wsclean_cmd(
    ms: MS,
    wsclean_options: WSCleanOptions,
) -> WSCleanResult:
    """Create a wsclean command from a WSCleanOptions container

    For the most part these are one-to-one mappings to the wsclean CLI with the
    exceptions being:
    #. the `-name` argument will be generated and supplied to the CLI string and will default to the parent directory and name of the supplied measurement set
    #. If `wsclean_options.temp_dir` is specified this directory is used in place of the measurement sets parent directory

    If `container` is supplied to immediately execute this command then the
    output wsclean image products will be moved from the `temp-dir` to the
    same directory as the measurement set.

    Args:
        ms (MS): The measurement set to be imaged
        wsclean_options (WSCleanOptions): WSClean options to image with
        container (Optional[Path], optional): If a path to a container is provided the command is executed immediately. Defaults to None.

    Raises:
        ValueError: Raised when a option has not been successfully processed

    Returns:
        WSCleanResult: The wsclean command to run
    """
    # TODO: This is very very smelly. I think removing the `.name` from WSCleanOptions
    # to start with, build that as an explicit testable function, and pass/return the name
    # argument alongside the prefix in the WSCleanCMD. Also need to rename that, its a horrible
    # name for a variable and ship

    # Some options should also extend the singularity bind directories
    bind_dir_paths = []

    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options, ms=ms
    )
    move_directory = ms.path.parent
    hold_directory: Path | None = Path(name_argument_path).parent

    wsclean_options_dict = wsclean_options._asdict()

    unknowns: list[tuple[Any, Any]] = []
    logger.info("Creating wsclean command.")

    cli_results = list(
        map(
            _resolve_wsclean_key_value_to_cli_str,
            wsclean_options_dict.keys(),
            wsclean_options_dict.values(),
        )
    )

    # Ignore any CLIResult if it has been explicitly instructed to
    cli_results = [cli_result for cli_result in cli_results if not cli_result.ignore]

    cmds = [cli_result.cmd for cli_result in cli_results if cli_result.cmd]
    unknowns = [cli_result.unknown for cli_result in cli_results if cli_result.unknown]
    bind_dir_paths += [
        cli_result.bindpath for cli_result in cli_results if cli_result.bindpath
    ]

    if len(unknowns) > 0:
        msg = ", ".join([f"{t[0]} {t[1]}" for t in unknowns])
        raise ValueError(f"Unknown wsclean option types: {msg}")

    cmds += [f"-name {name_argument_path!s}"]
    cmds += [f"{ms.path!s} "]

    bind_dir_paths.append(ms.path.parent)

    cmd = "wsclean " + " ".join(cmds)

    logger.info(f"Constructed wsclean command: {cmd=}")
    logger.info("Setting default model data column to 'MODEL_DATA'")

    return WSCleanResult(
        cmd=cmd,
        options=wsclean_options,
        ms=ms.with_options(model_column="MODEL_DATA"),
        bind_dirs=tuple(bind_dir_paths),
        move_hold_directories=(move_directory, hold_directory),
        image_prefix_str=str(name_argument_path),
    )


def rotate_cube(output_cube_path: str | Path, inplace: bool = True) -> Path:
    """
    Rotate the FITS cube axes to a shape of (chan, pol, dec, ra)
    which is what yandasoft linmos tasks expect.

    Parameters
    ----------
    output_cube_path : str | Path
        Path to the FITS cube to rotate.
    inplace : bool, optional
        If True, modify the file in-place. If False, write to a temporary file and
        then replace the original. Default True

    Returns
    -------
    Path
        Path to the rotated FITS cube.
    """
    output_path = Path(output_cube_path)
    logger.info(f"Rotating FITS axes of {output_path.name}")

    # Read original data and header
    with fits.open(output_path, mode="readonly", memmap=True) as hdul:
        header = hdul[0].header.copy()
        data_cube = hdul[0].data.copy()

    # Swap axes in header
    tmp_header = header.copy()
    for a, b in ((3, 4), (4, 3)):
        header[f"CTYPE{a}"] = tmp_header[f"CTYPE{b}"]
        header[f"CRPIX{a}"] = tmp_header[f"CRPIX{b}"]
        header[f"CRVAL{a}"] = tmp_header[f"CRVAL{b}"]
        header[f"CDELT{a}"] = tmp_header[f"CDELT{b}"]
        header[f"CUNIT{a}"] = tmp_header[f"CUNIT{b}"]

    # Move data axis: (pol, chan, dec, ra) → (chan, pol, dec, ra)
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


def combine_image_set_to_cube(
    image_set: ImageSet,
    remove_original_images: bool = False,
    inplace: bool = True,
) -> ImageSet:
    """Combine wsclean subband channel images into a cube. Each collection attribute
    of the input `image_set` will be inspected. The MFS images will be ignored.

    A output file name will be generated based on the  prefix and mode (e.g. `image`, `residual`, `psf`, `dirty`).

    Args:
        image_set (ImageSet): Collection of wsclean image productds
        remove_original_images (bool, optional): If True, images that went into the cube are removed. Defaults to False.
        inplace (bool, optional): If True, modify the file in-place. If False, write to a temporary file and
        then replace the original. Default True

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
            image for image in image_set_dict[mode] if "-MFS-" not in str(image)
        ]
        if len(subband_images) <= 1:
            logger.info(f"Not enough subband images for {mode=}, not creating a cube")
            continue

        output_cube_name = create_image_cube_name(
            image_prefix=Path(image_set.prefix), mode=mode
        )

        logger.info(f"Combining {len(subband_images)} images. {subband_images=}")
        freqs = combine_fits(file_list=subband_images, out_cube=output_cube_name)

        rotate_cube(output_cube_name, inplace=inplace)

        # Write out the hdu to preserve the beam table constructed in fitscube
        logger.info(f"Writing {output_cube_name=}")

        output_freqs_name = Path(output_cube_name).with_suffix(".freqs_Hz.txt")
        np.savetxt(output_freqs_name, freqs.to("Hz").value)

        image_set_dict[mode] = [Path(output_cube_name)] + [
            image for image in image_set_dict[mode] if image not in subband_images
        ]

        if remove_original_images:
            remove_files_folders(*subband_images)

    return ImageSet(**image_set_dict)


def rename_wsclean_prefix_in_image_set(input_image_set: ImageSet) -> ImageSet:
    """Given an input image_set, rename the files contained in it to
    remove the `-` separator that wsclean uses and replace it with `.`.

    Files will be renamed on disk appropriately.

    Args:
        input_image_set (ImageSet): The collection of output wsclean products

    Returns:
        ImageSet: The updated image_set after replacing the separator and renaming files
    """

    input_args = options_to_dict(input_options=input_image_set)

    check_keys = ("prefix", "image", "residual", "model", "dirty")

    output_args: dict[str, Any] = {}

    for key, value in input_args.items():
        if key == "prefix":
            output_args[key] = _rename_wsclean_title(name_str=value)
        elif key in check_keys and value is not None:
            output_args[key] = [
                _rename_wsclean_file(input_path=fits_path, rename_file=True)
                for fits_path in value
            ]
        else:
            output_args[key] = value

    output_image_set = ImageSet(**output_args)

    return output_image_set


def _make_pols(pol_str: str) -> tuple[str, ...] | None:
    """Create a tuple of polarisations from a polarisation string

    Args:
        pol_str (str): Polarisation string to convert

    Returns:
        tuple[str, ...] | None: Tuple of polarisations
    """
    if any(pol in pol_str.lower() for pol in ("x", "y", "r", "l")):
        msg = f"Found instrumental polarisation in {pol_str=}. This is not supported."
        raise NotSupportedError(msg)

    pols: tuple[str, ...] | None = None
    if len(pol_str) > 1:
        pols = tuple(p.upper() for p in "".join(pol_str.split(",")))
    return pols


def run_wsclean_imager(
    wsclean_result: WSCleanResult,
    container: Path,
    make_cube_from_subbands: bool = True,
) -> ImageSet:
    """Run a provided wsclean command. Optionally will clean up files,
    including the dirty beams, psfs and other assorted things.

    An `ImageSet` is constructed that attempts to capture the output wsclean image products. If `image_prefix_str`
    is specified the image set will be created by (ordered by preference):
    #. Adding the `image_prefix_str` to the `move_directory`
    #. Guessing it from the path of the measurement set from `wsclean_result.ms.path`

    Args:
        wsclean_result (WSCleanResult): The command to run, and other properties (cleanup.)
        container (Path): Path to the container with wsclean available in it
        bind_dirs (Optional[Tuple[Path, ...]], optional): Additional directories to include when binding to the wsclean container. Defaults to None.
        move_hold_directories (Optional[Tuple[Path,Optional[Path]]], optional): The `move_directory` and `hold_directory` passed to the temporary context manager. If None no `hold_then_move_into` manager is used. Defaults to None.
        make_cube_from_subbands (bool, optional): Form a single FITS cube from the set of sub-band images wsclean produces. Defaults to False.
        image_prefix_str (Optional[str], optional): The name used to search for wsclean outputs. If None, it is guessed from the name and location of the MS. Defaults to None.

    Returns:
        ImageSet: The executed wsclean output products.
    """

    ms = wsclean_result.ms
    single_channel = wsclean_result.options.channels_out == 1
    wsclean_cleanup = wsclean_result.cleanup
    bind_dirs = wsclean_result.bind_dirs
    move_hold_directories = wsclean_result.move_hold_directories
    image_prefix_str = wsclean_result.image_prefix_str

    sclient_bind_dirs = [Path(ms.path).parent.absolute()]
    if bind_dirs:
        sclient_bind_dirs = sclient_bind_dirs + list(bind_dirs)

    prefix = image_prefix_str if image_prefix_str else None
    if prefix is None:
        prefix = str(ms.path.parent / ms.path.name)
        logger.warning(f"Setting prefix to {prefix}. Likely this is not correct. ")

    if move_hold_directories:
        with hold_then_move_into(
            move_directory=move_hold_directories[0],
            hold_directory=move_hold_directories[1],
        ) as directory:
            sclient_bind_dirs.append(directory)
            run_singularity_command(
                image=container,
                command=wsclean_result.cmd,
                bind_dirs=sclient_bind_dirs,
                stream_callback_func=_wsclean_output_callback,
                ignore_logging_output=wsclean_result.options.flint_no_log_wsclean_output,
            )
            if wsclean_cleanup:
                rm_files = wsclean_cleanup_files(
                    prefix=prefix, single_channel=single_channel
                )
                logger.info(f"Removed {len(rm_files)} files")
                # No need to attempt to clean up again once files have been moved

                wsclean_cleanup = False
            # Update the prefix based on where the files will be moved to
            prefix = (
                f"{move_hold_directories[0] / Path(prefix).name!s}"
                if image_prefix_str
                else None
            )
    else:
        run_singularity_command(
            image=container,
            command=wsclean_result.cmd,
            bind_dirs=sclient_bind_dirs,
            stream_callback_func=_wsclean_output_callback,
            ignore_logging_output=wsclean_result.options.flint_no_log_wsclean_output,
        )

    # prefix should be set at this point
    assert prefix is not None, f"{prefix=}, which should not happen"

    if wsclean_cleanup:
        logger.info("Will clean up files created by wsclean. ")
        rm_files = wsclean_cleanup_files(prefix=prefix, single_channel=single_channel)

    pols = _make_pols(pol_str=wsclean_result.options.pol)

    image_set = get_wsclean_output_names(
        prefix=prefix,
        subbands=wsclean_result.options.channels_out,
        pols=pols,
        verify_exists=True,
        output_types=("image", "residual"),
        check_exists_when_adding=True,
    )

    if wsclean_result.options.save_source_list:
        logger.info("Attaching the wsclean clean components SEDs")
        source_list_path = get_wsclean_output_source_list_path(
            name_path=prefix, pol=None
        )
        assert source_list_path.exists(), f"{source_list_path=} does not exist"
        image_set = image_set.with_options(source_list=source_list_path)

    if make_cube_from_subbands:
        image_set = combine_image_set_to_cube(
            image_set=image_set,
            remove_original_images=True,
            inplace=wsclean_result.options.flint_make_cube_inplace,
        )

    image_set = rename_wsclean_prefix_in_image_set(input_image_set=image_set)

    logger.info(f"Constructed {image_set=}")

    return image_set


def wsclean_imager(
    ms: Path | MS,
    wsclean_container: Path,
    update_wsclean_options: dict[str, Any] | None = None,
    make_cube_from_subbands: bool = True,
) -> WSCleanResult:
    """Create and run a wsclean imager command against a measurement set.

    Args:
        ms (Union[Path,MS]): Path to the measurement set that will be imaged
        wsclean_container (Path): Path to the container with wsclean installed
        update_wsclean_options (Optional[Dict[str, Any]], optional): Additional options to update the generated WscleanOptions with. Keys should be attributes of WscleanOptions. Defaults to None.

    Returns:
        WSCleanResult: _description_
    """

    # TODO: This should be expanded to support multiple measurement sets
    ms = MS.cast(ms)

    wsclean_options = WSCleanOptions()
    if update_wsclean_options:
        logger.info("Updatting wsclean options with user-provided items. ")
        wsclean_options = wsclean_options.with_options(**update_wsclean_options)

    assert ms.column is not None, "A MS column needs to be elected for imaging. "
    wsclean_options = wsclean_options.with_options(data_column=ms.column)
    wsclean_result = create_wsclean_cmd(
        ms=ms,
        wsclean_options=wsclean_options,
    )
    image_set = run_wsclean_imager(
        wsclean_result=wsclean_result,
        container=wsclean_container,
        make_cube_from_subbands=make_cube_from_subbands,
    )

    return wsclean_result.with_options(image_set=image_set)


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Routines related to wsclean")

    subparser = parser.add_subparsers(dest="mode")

    wsclean_parser = subparser.add_parser(
        "image", help="Attempt to run a wsclean command. "
    )
    wsclean_parser.add_argument(
        "ms", type=Path, help="Path to a measurement set to image"
    )
    wsclean_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Extra output logging."
    )
    wsclean_parser.add_argument(
        "--wsclean-container",
        type=Path,
        default=None,
        help="Path to a singularity container with wsclean installed. ",
    )
    wsclean_parser = add_options_to_parser(
        parser=wsclean_parser, options_class=WSCleanOptions
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "image":
        if args.verbose:
            import logging

            logger.setLevel(logging.DEBUG)

        ms = MS(path=args.ms, column=args.data_column)
        wsclean_options: WSCleanOptions = create_options_from_parser(
            parser_namespace=args,
            options_class=WSCleanOptions,  # type: ignore
        )
        wsclean_imager(
            ms=ms,
            wsclean_container=args.wsclean_container,
            update_wsclean_options=wsclean_options._asdict(),
        )


if __name__ == "__main__":
    cli()
