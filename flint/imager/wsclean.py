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
from typing import Any, Collection, Dict, List, NamedTuple, Optional, Tuple, Union

import numpy as np
from fitscube.combine_fits import combine_fits

from flint.exceptions import CleanDivergenceError
from flint.logging import logger
from flint.ms import MS
from flint.naming import create_image_cube_name, create_imaging_name_prefix
from flint.options import (
    options_to_dict,
    BaseOptions,
    add_options_to_parser,
    create_options_from_parser,
)
from flint.sclient import run_singularity_command
from flint.utils import (
    get_environment_variable,
    hold_then_move_into,
    remove_files_folders,
)


class ImageSet(BaseOptions):
    """A structure to represent the images and auxiliary products produced by
    wsclean"""

    prefix: str
    """Prefix of the images and other output products. This should correspond to the -name argument from wsclean"""
    image: List[Path]
    """Images produced. """
    psf: Optional[List[Path]] = None
    """References to the PSFs produced by wsclean. """
    dirty: Optional[List[Path]] = None
    """Dirty images. """
    model: Optional[List[Path]] = None
    """Model images.  """
    residual: Optional[List[Path]] = None
    """Residual images."""
    source_list: Optional[Path] = None
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
    local_rms_window: int = 65
    """Size of the window used to estimate rms noise"""
    size: int = 10128
    """Image size, only a single dimension is required. Note that this means images will be squares. """
    local_rms: bool = True
    """Whether a local rms map is computed"""
    force_mask_rounds: int = 10
    """Round of force masked derivation"""
    auto_mask: float = 3.5
    """How deep the construct clean mask is during each cycle"""
    auto_threshold: float = 0.5
    """How deep to clean once initial clean threshold reached"""
    threshold: Optional[float] = None
    """Threshold in Jy to stop cleaning"""
    channels_out: int = 4
    """Number of output channels"""
    mgain: float = 0.7
    """Major cycle gain"""
    nmiter: int = 15
    """Maximum number of major cycles to perform"""
    niter: int = 750000
    """Maximum number of minor cycles"""
    multiscale: bool = True
    """Enable multiscale deconvolution"""
    multiscale_scale_bias: float = 0.75
    """Multiscale bias term"""
    multiscale_gain: Optional[float] = None
    """Size of step made in the subminor loop of multi-scale. Default currently 0.2, but shows sign of instability. A value of 0.1 might be more stable."""
    multiscale_scales: Tuple[int, ...] = (
        0,
        15,
        25,
        50,
        75,
        100,
        250,
        400,
    )
    """Scales used for multi-scale deconvolution"""
    fit_spectral_pol: Optional[int] = None
    """Number of spectral terms to include during sub-band subtraction"""
    weight: str = "briggs -0.5"
    """Robustness of the weighting used"""
    data_column: str = "CORRECTED_DATA"
    """Which column in the MS to image"""
    scale: str = "2.5asec"
    """Pixel scale size"""
    gridder: Optional[str] = "wgridder"
    """Use the wgridder kernel in wsclean (instead of the default w-stacking method)"""
    nwlayers: Optional[int] = None
    """Number of w-layers to use if the gridder mode is w-stacking"""
    wgridder_accuracy: float = 1e-4
    """The accuracy requested of the wgridder (should it be used), compared as the RMS error when compred to a DFT"""
    join_channels: bool = True
    """Collapse the sub-band images down to an MFS image when peak-finding"""
    minuv_l: Optional[float] = None
    """The minimum lambda length that the visibility data needs to meet for it to be selected for imaging"""
    minuvw_m: Optional[float] = None
    """A (u,v) selection command, where any baselines shorter than this will be ignored during imaging"""
    maxw: Optional[float] = None
    """A percentage specifying the maximum w-term to be gridded, relative to the max w-term being considered"""
    no_update_model_required: bool = False
    """Will instruct wsclean not to create the MODEL_DATA column"""
    no_small_inversion: bool = False
    """Disables an optimisation of wsclean's w-gridder mode. This might improve accuracy of the w-gridder. """
    beam_fitting_size: Optional[float] = 1.25
    """Use a fitting box the size of <factor> times the theoretical beam size for fitting a Gaussian to the PSF."""
    fits_mask: Optional[Path] = None
    """Path to a FITS file that encodes a cleaning mask"""
    deconvolution_channels: Optional[int] = None
    """The channels out will be averaged down to this many sub-band images during deconvolution"""
    parallel_deconvolution: Optional[int] = None
    """If not none, then this is the number of sub-regions wsclean will attempt to divide and clean"""
    parallel_gridding: Optional[int] = None
    """If not none, then this is the number of channel images that will be gridded in parallel"""
    temp_dir: Optional[Union[str, Path]] = None
    """The path to a temporary directory where files will be wrritten. """
    pol: str = "i"
    """The polarisation to be imaged"""
    save_source_list: bool = False
    """Saves the found clean components as a BBS/DP3 text sky model"""


class WSCleanCommand(BaseOptions):
    """Simple container for a wsclean command."""

    cmd: str
    """The constructede wsclean command that would be executed."""
    options: WSCleanOptions
    """The set of wslean options used for imaging"""
    ms: MS
    """The measurement sets that have been included in the wsclean command. """
    imageset: Optional[ImageSet] = None
    """Collection of images produced by wsclean"""
    cleanup: bool = True
    """Will clean up the dirty images/psfs/residuals/models when the imaging has completed"""


def get_wsclean_output_source_list_path(
    name_path: Union[str, Path], pol: Optional[str] = None
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
    """Construct an apply a regular expression that aims to identify
    the wsclean appended properties string within a file and replace
    the `-` separator with a `.`.

    Args:
        name_str (str): The name that will be extracted and modified

    Returns:
        str: The modified string if a wsclean string was matched, otherwise the input `name-str`
    """
    search_re = r"(-(i|q|u|v|xx|xy|yx|yy))?-((MFS|[0-9]{4}))(-t[0-9]{5})?-(image|dirty|model|residual|psf)"
    match_re = re.compile(search_re)

    logger.info(f"{name_str=} {type(name_str)=}")
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


# TODO: Update this function to also add int the source list
def get_wsclean_output_names(
    prefix: str,
    subbands: int,
    pols: Optional[Union[str, Tuple[str]]] = None,
    verify_exists: bool = False,
    include_mfs: bool = True,
    output_types: Union[str, Collection[str]] = (
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
    # TODO: Use a regular expression for this
    subband_strs = [f"{subband:04}" for subband in range(subbands)]
    if include_mfs:
        subband_strs.append("MFS")

    in_pols: Tuple[Union[None, str]]
    if pols is None:
        in_pols = (None,)
    elif isinstance(pols, str):
        in_pols = (pols,)
    else:
        in_pols = pols

    if isinstance(output_types, str):
        output_types = (output_types,)

    images: Dict[str, List[Path]] = {}
    for image_type in ("image", "dirty", "model", "residual"):
        if image_type not in output_types:
            continue

        paths: List[Path] = []
        for pol in in_pols:
            for subband_str in subband_strs:
                if pol:
                    path_str = f"{prefix}-{subband_str}-{pol}-{image_type}.fits"
                else:
                    path_str = f"{prefix}-{subband_str}-{image_type}.fits"

                if check_exists_when_adding and not Path(path_str).exists():
                    logger.debug(f"{path_str} does not existing. Not adding. ")
                    continue

                paths.append(Path(path_str))

        images[image_type] = paths

    # The PSF is the same for all stokes
    if "psf" in output_types:
        psf_images = [
            Path(f"{prefix}-{subband_str}-psf.fits") for subband_str in subband_strs
        ]
        # Filter out files if they do not exists
        if check_exists_when_adding:
            psf_images = [psf_image for psf_image in psf_images if psf_image.exists()]

        images["psf"] = psf_images

    if verify_exists:
        paths_no_exists: List[Path] = []
        for _, check_paths in images.items():
            paths_no_exists += [path for path in check_paths if not path.exists()]
        if len(paths_no_exists) > 0:
            raise FileExistsError(
                f"The following {len(paths_no_exists)} files do not exist: {paths_no_exists}"
            )

    return ImageSet(prefix=prefix, **images)


def delete_wsclean_outputs(
    prefix: str, output_type: str = "image", ignore_mfs: bool = True
) -> Collection[Path]:
    """Attempt to remove elected wsclean output files

    Args:
        prefix (str): The prefix of the files to remove. This would correspond to the -name of wsclean.
        output_type (str, optional): What type of wsclean output to try to remove. Defaults to 'image'.
        ignore_mfs (bool, optional): If True, do not remove MFS outputs (attempt to, at least). Defaults to True.

    Returns:
        Collection[Path]: The paths that were removed (or at least attempted to be removed)/
    """
    # TODO: This glob needs to be replaced with something more explicit
    paths = [Path(p) for p in glob(f"{prefix}-*{output_type}.fits")]
    logger.info(f"Found {len(paths)} matching {prefix=} and {output_type=}.")
    rm_paths: List[Path] = []

    for path in paths:
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
    pol = wsclean_options_dict["pol"]
    name_prefix_str = create_imaging_name_prefix(ms=ms, pol=pol)

    # Now resolve the directory part
    name_dir: Union[Path, str, None] = ms.path.parent
    temp_dir = wsclean_options_dict.get("temp_dir", None)
    if temp_dir:
        # Resolve if environment variable
        name_dir = (
            get_environment_variable(variable=temp_dir)
            if isinstance(temp_dir, str) and temp_dir[0] == "$"
            else Path(temp_dir)
        )

    assert name_dir is not None, f"{name_dir=} is None, which is bad"

    name_argument_path = Path(name_dir) / name_prefix_str
    logger.info(f"Constructed -name {name_argument_path}")

    return name_argument_path


class ResolvedCLIResult(NamedTuple):
    """Mapping results to provide to wsclean"""

    cmd: Optional[str] = None
    """The argument value pair to place on the CLI"""
    unknown: Optional[Any] = None
    """Unknown options that could not be converted"""
    bindpath: Optional[Path] = None
    """A path to bind to when called within a container"""


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
        get_environment_variable(variable=value)
        if isinstance(value, str) and value[0] == "$"
        else value
    )

    cmd = None
    unknown = None
    bind_dir_path = None

    original_key = key
    key = key.replace("_", "-")

    if key == "size":
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
    container: Optional[Path] = None,
) -> WSCleanCommand:
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
        WSCleanCommand: The wsclean command to run
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
    hold_directory: Optional[Path] = Path(name_argument_path).parent

    wsclean_options_dict = wsclean_options._asdict()

    unknowns: List[Tuple[Any, Any]] = []
    logger.info("Creating wsclean command.")

    cli_results = map(
        _resolve_wsclean_key_value_to_cli_str,
        wsclean_options_dict.keys(),
        wsclean_options_dict.values(),
    )
    cmds = [cli_result.cmd for cli_result in cli_results if cli_result.cmd]
    unknowns = [cli_result.unknown for cli_result in cli_results if cli_result.unknown]
    bind_dir_paths += [
        cli_result.bindpath for cli_result in cli_results if cli_result.bindpath
    ]

    if len(unknowns) > 0:
        msg = ", ".join([f"{t[0]} {t[1]}" for t in unknowns])
        raise ValueError(f"Unknown wsclean option types: {msg}")

    cmds += [f"-name {str(name_argument_path)}"]
    cmds += [f"{str(ms.path)} "]

    bind_dir_paths.append(ms.path.parent)

    cmd = "wsclean " + " ".join(cmds)

    logger.info(f"Constructed wsclean command: {cmd=}")
    logger.info("Setting default model data column to 'MODEL_DATA'")
    wsclean_cmd = WSCleanCommand(
        cmd=cmd, options=wsclean_options, ms=ms.with_options(model_column="MODEL_DATA")
    )

    if container:
        wsclean_cmd = run_wsclean_imager(
            wsclean_cmd=wsclean_cmd,
            container=container,
            bind_dirs=tuple(bind_dir_paths),
            move_hold_directories=(move_directory, hold_directory),
            image_prefix_str=str(name_argument_path),
        )

    return wsclean_cmd


def combine_subbands_to_cube(
    imageset: ImageSet,
    remove_original_images: bool = False,
) -> ImageSet:
    """Combine wsclean subband channel images into a cube. Each collection attribute
    of the input `imageset` will be inspected. The MFS images will be ignored.

    A output file name will be generated based on the  prefix and mode (e.g. `image`, `residual`, `psf`, `dirty`).

    Args:
        imageset (ImageSet): Collection of wsclean image productds
        remove_original_images (bool, optional): If True, images that went into the cube are removed. Defaults to False.

    Returns:
        ImageSet: Updated iamgeset describing the new outputs
    """
    logger.info("Combining subband image products into fits cubes")

    if not isinstance(imageset, ImageSet):
        raise TypeError(
            f"Input imageset of type {type(imageset)}, expect {type(ImageSet)}"
        )

    imageset_dict = options_to_dict(input_options=imageset)

    for mode in ("image", "residual", "dirty", "model", "psf"):
        if imageset_dict[mode] is None or not isinstance(
            imageset_dict[mode], (list, tuple)
        ):
            logger.info(f"{mode=} is None or not appropriately formed. Skipping. ")
            continue

        subband_images = [
            image for image in imageset_dict[mode] if "-MFS-" not in str(image)
        ]
        if len(subband_images) <= 1:
            logger.info(f"Not enough subband images for {mode=}, not creating a cube")
            continue

        logger.info(f"Combining {len(subband_images)} images. {subband_images=}")
        hdu1, freqs = combine_fits(file_list=subband_images)

        # This changes the output cube to a shape of (chan, pol, dec, ra)
        # which is what yandasoft linmos tasks like
        new_header = hdu1[0].header  # type: ignore
        data_cube = hdu1[0].data  # type: ignore

        tmp_header = new_header.copy()
        # Need to swap NAXIS 3 and 4 to make LINMOS happy - booo
        for a, b in ((3, 4), (4, 3)):
            new_header[f"CTYPE{a}"] = tmp_header[f"CTYPE{b}"]
            new_header[f"CRPIX{a}"] = tmp_header[f"CRPIX{b}"]
            new_header[f"CRVAL{a}"] = tmp_header[f"CRVAL{b}"]
            new_header[f"CDELT{a}"] = tmp_header[f"CDELT{b}"]
            new_header[f"CUNIT{a}"] = tmp_header[f"CUNIT{b}"]

        # Cube is currently STOKES, FREQ, RA, DEC - needs to be FREQ, STOKES, RA, DEC
        data_cube = np.moveaxis(data_cube, 1, 0)
        hdu1[0].data = data_cube  # type: ignore
        hdu1[0].header = new_header  # type: ignore

        output_cube_name = create_image_cube_name(
            image_prefix=Path(imageset.prefix), mode=mode
        )

        # Write out the hdu to preserve the beam table constructed in fitscube
        logger.info(f"Writing {output_cube_name=}")
        hdu1.writeto(output_cube_name)

        output_freqs_name = Path(output_cube_name).with_suffix(".freqs_Hz.txt")
        np.savetxt(output_freqs_name, freqs.to("Hz").value)

        imageset_dict[mode] = [Path(output_cube_name)] + [
            image for image in imageset_dict[mode] if image not in subband_images
        ]

        if remove_original_images:
            remove_files_folders(*subband_images)

    return ImageSet(**imageset_dict)


def rename_wsclean_prefix_in_imageset(input_imageset: ImageSet) -> ImageSet:
    """Given an input imageset, rename the files contained in it to
    remove the `-` separator that wsclean uses and replace it with `.`.

    Files will be renamed on disk appropriately.

    Args:
        input_imageset (ImageSet): The collection of output wsclean products

    Returns:
        ImageSet: The updated imageset after replacing the separator and renaming files
    """

    input_args = options_to_dict(input_options=input_imageset)

    check_keys = ("prefix", "image", "residual", "model", "dirty")

    output_args: Dict[str, Any] = {}

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

    output_imageset = ImageSet(**output_args)

    return output_imageset


def run_wsclean_imager(
    wsclean_cmd: WSCleanCommand,
    container: Path,
    bind_dirs: Optional[Tuple[Path, ...]] = None,
    move_hold_directories: Optional[Tuple[Path, Optional[Path]]] = None,
    make_cube_from_subbands: bool = True,
    image_prefix_str: Optional[str] = None,
) -> WSCleanCommand:
    """Run a provided wsclean command. Optionally will clean up files,
    including the dirty beams, psfs and other assorted things.

    An `ImageSet` is constructed that attempts to capture the output wsclean image products. If `image_prefix_str`
    is specified the image set will be created by (ordered by preference):
    #. Adding the `image_prefix_str` to the `move_directory`
    #. Guessing it from the path of the measurement set from `wsclean_cmd.ms.path`

    Args:
        wsclean_cmd (WSCleanCommand): The command to run, and other properties (cleanup.)
        container (Path): Path to the container with wsclean available in it
        bind_dirs (Optional[Tuple[Path, ...]], optional): Additional directories to include when binding to the wsclean container. Defaults to None.
        move_hold_directories (Optional[Tuple[Path,Optional[Path]]], optional): The `move_directory` and `hold_directory` passed to the temporary context manager. If None no `hold_then_move_into` manager is used. Defaults to None.
        make_cube_from_subbands (bool, optional): Form a single FITS cube from the set of sub-band images wsclean produces. Defaults to False.
        image_prefix_str (Optional[str], optional): The name used to search for wsclean outputs. If None, it is guessed from the name and location of the MS. Defaults to None.

    Returns:
        WSCleanCommand: The executed wsclean command with a populated imageset properter.
    """

    ms = wsclean_cmd.ms

    sclient_bind_dirs = [Path(ms.path).parent.absolute()]
    if bind_dirs:
        sclient_bind_dirs = sclient_bind_dirs + list(bind_dirs)

    if move_hold_directories:
        with hold_then_move_into(
            move_directory=move_hold_directories[0],
            hold_directory=move_hold_directories[1],
        ) as directory:
            sclient_bind_dirs.append(directory)
            run_singularity_command(
                image=container,
                command=wsclean_cmd.cmd,
                bind_dirs=sclient_bind_dirs,
                stream_callback_func=_wsclean_output_callback,
            )

            # Update the prefix based on where the files will be moved to
            image_prefix_str = (
                f"{str(move_hold_directories[0] / Path(image_prefix_str).name)}"
                if image_prefix_str
                else None
            )
    else:
        run_singularity_command(
            image=container,
            command=wsclean_cmd.cmd,
            bind_dirs=sclient_bind_dirs,
            stream_callback_func=_wsclean_output_callback,
        )

    prefix = image_prefix_str if image_prefix_str else None
    if prefix is None:
        prefix = str(ms.path.parent / ms.path.name)
        logger.warning(f"Setting prefix to {prefix}. Likely this is not correct. ")

    if wsclean_cmd.cleanup:
        logger.info("Will clean up files created by wsclean. ")

        for output_type in ("dirty", "psf", "model", "residual"):
            delete_wsclean_outputs(prefix=prefix, output_type=output_type)
        # for output_type in ("model", "residual"):
        #     delete_wsclean_outputs(
        #         prefix=prefix, output_type=output_type, ignore_mfs=False
        #     )

    imageset = get_wsclean_output_names(
        prefix=prefix,
        subbands=wsclean_cmd.options.channels_out,
        verify_exists=True,
        output_types=("image", "residual"),
        check_exists_when_adding=True,
    )

    if wsclean_cmd.options.save_source_list:
        logger.info("Attaching the wsclean clean components SEDs")
        source_list_path = get_wsclean_output_source_list_path(
            name_path=prefix, pol=None
        )
        assert source_list_path.exists(), f"{source_list_path=} does not exist"
        imageset = imageset.with_options(source_list=source_list_path)

    if make_cube_from_subbands:
        imageset = combine_subbands_to_cube(
            imageset=imageset, remove_original_images=True
        )

    imageset = rename_wsclean_prefix_in_imageset(input_imageset=imageset)

    logger.info(f"Constructed {imageset=}")

    return wsclean_cmd.with_options(imageset=imageset)


def wsclean_imager(
    ms: Union[Path, MS],
    wsclean_container: Path,
    update_wsclean_options: Optional[Dict[str, Any]] = None,
) -> WSCleanCommand:
    """Create and run a wsclean imager command against a measurement set.

    Args:
        ms (Union[Path,MS]): Path to the measurement set that will be imaged
        wsclean_container (Path): Path to the container with wsclean installed
        update_wsclean_options (Optional[Dict[str, Any]], optional): Additional options to update the generated WscleanOptions with. Keys should be attributes of WscleanOptions. Defaults to None.

    Returns:
        WSCleanCommand: _description_
    """

    # TODO: This should be expanded to support multiple measurement sets
    ms = MS.cast(ms)

    wsclean_options = WSCleanOptions()
    if update_wsclean_options:
        logger.info("Updatting wsclean options with user-provided items. ")
        wsclean_options = wsclean_options.with_options(**update_wsclean_options)

    assert ms.column is not None, "A MS column needs to be elected for imaging. "
    wsclean_options = wsclean_options.with_options(data_column=ms.column)
    wsclean_cmd = create_wsclean_cmd(
        ms=ms,
        wsclean_options=wsclean_options,
        container=wsclean_container,
    )

    return wsclean_cmd


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
