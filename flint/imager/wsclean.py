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
from collections.abc import Collection
from numbers import Number
from pathlib import Path
from typing import Any, NamedTuple

from capn_crunch import (
    add_options_to_parser,
    create_options_from_parser,
)
from pydantic import Field, model_validator

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
    NotSupportedError,
)
from flint.imageset import (
    ImageSet,
    combine_image_set_to_cube,
    form_stokes_i,
    merge_image_sets,
)
from flint.logging import logger
from flint.ms import MS, standardise_ms_to_list_ms
from flint.naming import (
    create_imaging_name_prefix,
    pol_name_token,
    replace_name_field,
)
from flint.options import (
    BaseOptions,
    FitsCubeOptions,
)
from flint.sclient import run_singularity_command
from flint.utils import (
    hold_then_move_into,
    parse_environment_variables,
    remove_files_folders,
)


class WSCleanOptions(BaseOptions):
    """A basic container to handle WSClean options. These attributes should
    conform to the same option name in the calling signature of wsclean

    Basic support for environment variables is available. Should a value start
    with `$` it is assumed to be a environment variable, it is will be looked up.
    Some basic attempts to determine if it is a path is made.

    Should `flint_hold_dir` (or, failing that, `temp_dir`) be specified then all
    images will be created in this location, and then moved over to the same
    parent directory as the imaged MS. This is done by setting the wsclean
    `-name` argument.

    Options that start with `flint_` are ignored when generating the wsclean command
    and generally denote some other flint specific operation.
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
    channel_division_frequencies: tuple[float, ...] | None = None
    """Frequencies, in Hz, that divide the output channels into groups. Supplying ``channels_out - 1`` values fully specifies the output frequency grid, see ``flint.imager.channel_division``"""
    gain: float = 0.1
    """Cleaning gain, ratio of peak that will be subtracted in each iteration"""
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
    multiscale_fits_mask: Path | None = None
    """Path to a FITS file that contains per-scale clean masks encoded as per-pixel bit values"""
    deconvolution_channels: int | None = None
    """The channels out will be averaged down to this many sub-band images during deconvolution"""
    parallel_deconvolution: int | None = None
    """If not none, the maximum sub-image size in pixels used during deconvolution (suggestion between to between 1024-4096)"""
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
    interval: tuple[int, int] | None = None
    """Image a set of scans between a lower (inclusive) and upper (exclusive) bound"""
    no_reorder: bool = False
    """If True turn off the reordering of the MS at the beginning of wsclean"""
    no_mf_weighting: bool = False
    """Opposite of -ms-weighting; can be used to turn off MF weighting in -join-channels mode"""
    verbose: bool = False
    """Increase logging output"""
    deconvolution_threads: int | None = None
    """Number of threads to use during the deconvolution. More may make things faster but can come with a memory hit. If None defaults to -j"""
    taper_gaussian: float | None = None
    """The size of a Gaussian function applied to the weights, assuming units of arcseconds. Defaults to None. """
    # Options below here are not added to wsclean command
    flint_no_log_wsclean_output: bool = False
    """If True do not log the wsclean output"""
    flint_name_suffix: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9]+$")
    """An additional trailing token appended to the constructed wsclean ``-name``, e.g. to disambiguate outputs from different pipelines imaging the same measurement set. Letters and digits only, as that is all the name parser reads back from the ``project`` field"""
    flint_save_mfs_products: bool = False
    """Save the MFS image, model and residual products (co-added and leakage-corrected the same way as the science image), rather than just the image"""
    flint_hold_dir: str | Path | None = None
    """Directory images are written to before being moved beside the MS. Takes precedence over ``temp_dir`` for this purpose"""
    flint_keep_linear_images: bool = False
    """Keep the XX and YY images when ``pol`` is ``xx,yy``, rather than deleting them once Stokes I is formed"""

    @model_validator(mode="after")
    def check_pol(self) -> WSCleanOptions:
        """Check that the polarisations can be imaged, and that no source list is
        asked of XX and YY imaging.

        Raises:
            ValueError: ``pol`` is not supported, or ``save_source_list`` is set with ``pol='xx,yy'``

        Returns:
            WSCleanOptions: These options, unchanged
        """
        try:
            pols = parse_pols(pol_str=self.pol)
        except NotSupportedError as e:
            raise ValueError(str(e)) from e
        if pols == ("XX", "YY") and self.save_source_list:
            msg = (
                "save_source_list is not supported with pol='xx,yy'. wsclean only "
                "saves a source list when a single polarisation is imaged (Stokes I, "
                "or pseudo Stokes I: XX, YY, LL or RR)"
            )
            raise ValueError(msg)
        return self


class WSCleanResult(BaseOptions):
    """Simple container for a wsclean command."""

    cmd: str
    """The constructede wsclean command that would be executed."""
    options: WSCleanOptions
    """The set of wslean options used for imaging"""
    ms: MS
    """The base measurement sets that have been included in the wsclean command. """
    ms_list: list[MS]
    """List of measurement sets uused in the wsclean command"""
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


class WSCleanOutput(NamedTuple):
    """An image wsclean writes, with the fields of its name"""

    path: Path
    """Where the image is"""
    image_type: str
    """One of image, dirty, model, residual or psf"""
    channel: int | None = None
    """Output channel index. None when a single channel is imaged, or for an MFS image"""
    mfs: bool = False
    """Whether this is the multi-frequency synthesis image over all channels"""
    pol: str | None = None
    """wsclean's polarisation token, e.g. ``XX`` or ``Q``. None when a single polarisation is imaged"""


def wsclean_output_path(
    prefix: str | Path,
    image_type: str,
    channel: int | None = None,
    mfs: bool = False,
    pol: str | None = None,
) -> Path:
    """The name wsclean gives an image, ``<prefix>[-<channel>|-MFS][-<pol>]-<type>.fits``.

    Args:
        prefix (str | Path): The ``-name`` given to wsclean
        image_type (str): One of image, dirty, model, residual or psf
        channel (int | None, optional): Output channel index. None when a single channel is imaged. Defaults to None.
        mfs (bool, optional): Name the multi-frequency synthesis image, in place of a channel. Defaults to False.
        pol (str | None, optional): wsclean's polarisation token, e.g. ``XX``. None when a single polarisation is imaged. Defaults to None.

    Returns:
        Path: The image's path, in the directory of ``prefix``
    """
    fields = [str(prefix)]
    if mfs:
        fields.append("MFS")
    elif channel is not None:
        fields.append(f"{channel:04d}")
    if pol is not None:
        fields.append(pol)
    fields.append(image_type)

    return Path("-".join(fields) + ".fits")


def expected_wsclean_outputs(
    prefix: str | Path,
    channels_out: int,
    pols: tuple[str, ...] | None,
    image_types: Collection[str] = ("image",),
) -> list[WSCleanOutput]:
    """The images wsclean writes for each of ``image_types``. Every channel is
    listed, followed by the MFS image when more than one channel is imaged. The
    PSF is shared by every polarisation, so it carries none.

    Args:
        prefix (str | Path): The ``-name`` given to wsclean
        channels_out (int): Number of output channels imaged
        pols (tuple[str, ...] | None): wsclean's polarisation tokens, e.g. ``("Q", "U")``. None when a single polarisation is imaged.
        image_types (Collection[str], optional): The types of image to list. Defaults to ("image",).

    Returns:
        list[WSCleanOutput]: The images, by type, then polarisation, then channel
    """
    channels: list[dict[str, Any]] = [{}]
    if channels_out > 1:
        channels = [{"channel": channel} for channel in range(channels_out)]
        channels.append({"mfs": True})

    outputs: list[WSCleanOutput] = []
    for image_type in image_types:
        image_pols = (None,) if pols is None or image_type == "psf" else pols
        for pol in image_pols:
            for channel in channels:
                fields = {"image_type": image_type, "pol": pol, **channel}
                path = wsclean_output_path(prefix=prefix, **fields)
                outputs.append(WSCleanOutput(path=path, **fields))

    return outputs


def find_wsclean_outputs(
    prefix: str | Path,
    channels_out: int,
    pols: tuple[str, ...] | None,
) -> list[WSCleanOutput]:
    """Find the images this run of wsclean writes that exist under ``prefix``. Only
    their exact names are checked, so files from a run with other settings are ignored.

    Args:
        prefix (str | Path): The ``-name`` given to wsclean, in the directory the images are in
        channels_out (int): Number of output channels imaged
        pols (tuple[str, ...] | None): wsclean's polarisation tokens, e.g. ``("Q", "U")``. None when a single polarisation is imaged.

    Returns:
        list[WSCleanOutput]: The images that exist, by type, then polarisation, then channel, with the MFS image last
    """
    outputs = expected_wsclean_outputs(
        prefix=prefix,
        channels_out=channels_out,
        pols=pols,
        image_types=("image", "dirty", "model", "residual", "psf"),
    )
    return [output for output in outputs if output.path.exists()]


def clear_wsclean_outputs(
    prefix: str | Path, channels_out: int, pols: tuple[str, ...] | None
) -> list[Path]:
    """Delete any image this run of wsclean writes that already exists under ``prefix``.

    Args:
        prefix (str | Path): The ``-name`` given to wsclean, in the directory to clear
        channels_out (int): Number of output channels imaged
        pols (tuple[str, ...] | None): wsclean's polarisation tokens, e.g. ``("Q", "U")``. None when a single polarisation is imaged.

    Returns:
        list[Path]: The images deleted
    """
    outputs = find_wsclean_outputs(prefix=prefix, channels_out=channels_out, pols=pols)
    return remove_files_folders(*(output.path for output in outputs))


def verify_wsclean_outputs(
    outputs: list[WSCleanOutput],
    prefix: str | Path,
    channels_out: int,
    pols: tuple[str, ...] | None,
) -> None:
    """Raise if an image wsclean should have written is not among ``outputs``.

    Only the ``image`` type is checked, as cleaning up deletes the others.

    Args:
        outputs (list[WSCleanOutput]): The images found, see ``find_wsclean_outputs``
        prefix (str | Path): The ``-name`` given to wsclean
        channels_out (int): Number of output channels imaged
        pols (tuple[str, ...] | None): wsclean's polarisation tokens, e.g. ``("Q", "U")``. None when a single polarisation is imaged.

    Raises:
        FileNotFoundError: Some expected images are missing
    """
    found = {output.path.name for output in outputs}
    missing = [
        expected.path.name
        for expected in expected_wsclean_outputs(
            prefix=prefix, channels_out=channels_out, pols=pols
        )
        if expected.path.name not in found
    ]
    if missing:
        msg = f"wsclean did not write {len(missing)} expected images: {missing}"
        raise FileNotFoundError(msg)


def flint_output_path(
    output: WSCleanOutput, prefix: str | Path, pol_token: str
) -> Path:
    """The flint name of an image wsclean wrote, its fields separated by ``.``.

    When wsclean tagged the image with a polarisation it replaces ``pol_token``,
    the field of ``prefix`` naming every polarisation imaged (e.g. ``qu``).
    A channel index becomes flint's ``ch<lo>-<hi>`` range, whose upper bound is
    exclusive.

    Args:
        output (WSCleanOutput): The image to name
        prefix (str | Path): The ``-name`` given to wsclean
        pol_token (str): The field of ``prefix`` naming every polarisation imaged, see ``pol_name_token``

    Raises:
        NamingException: ``pol_token`` is not a field of ``prefix``

    Returns:
        Path: The flint name, in the directory of ``output``
    """
    prefix_path = Path(prefix)
    if output.pol is not None:
        prefix_path = replace_name_field(
            path=prefix_path, old=pol_token, new=output.pol.lower()
        )
    fields = prefix_path.name.split(".")
    if output.mfs:
        fields.append("MFS")
    elif output.channel is not None:
        fields.append(f"ch{output.channel:04d}-{output.channel + 1:04d}")
    fields.append(output.image_type)

    return output.path.parent / (".".join(fields) + ".fits")


def remove_wsclean_outputs(
    outputs: list[WSCleanOutput],
    image_types: Collection[str] = ("dirty", "psf", "model", "residual"),
) -> list[Path]:
    """Delete the channel images of ``image_types``, keeping every MFS image.
    A failed deletion is logged rather than raised.

    Args:
        outputs (list[WSCleanOutput]): The images wsclean wrote, see ``find_wsclean_outputs``
        image_types (Collection[str], optional): The types of image to delete. Defaults to ("dirty", "psf", "model", "residual").

    Returns:
        list[Path]: The images deleted
    """
    removed: list[Path] = []
    for output in outputs:
        if output.mfs or output.image_type not in image_types:
            continue
        logger.info(f"Removing {output.path}")
        try:
            output.path.unlink()
            removed.append(output.path)
        except Exception as e:
            logger.critical(f"Removing {output.path} failed: {e}")

    return removed


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


def create_wsclean_name_argument(
    wsclean_options: WSCleanOptions, ms: MS | list[MS]
) -> Path:
    """Create the value that will be provided to wsclean -name argument. This has
    to be generated. Among things to consider is the desired output directory of imaging
    files. This by default will be alongside the measurement set, and is
    overridden by `flint_hold_dir`, or failing that `temp_dir`.

    If the input ``ms`` is an instance of ``MSs`` then the first measurement
    set will be used to base the name from.

    Args:
        wsclean_options (WSCleanOptions): Set of wsclean options to consider
        ms (MS | list[MS]): The measurement set to be imaged

    Returns:
        Path: Value of the -name argument to provide to wsclean
    """
    wsclean_options_dict = wsclean_options._asdict()

    # Extract the first measurement set should multiple be provided
    name_ms: MS = ms if isinstance(ms, MS) else ms[0]

    # Prepare the name for the output wsclean command
    # Construct the name property of the string
    pol = wsclean_options.pol
    channel_range = wsclean_options.channel_range
    scan_range = wsclean_options.interval
    name_prefix_str = create_imaging_name_prefix(
        ms_path=name_ms.path,
        pol=pol,
        channel_range=channel_range,
        scan_range=scan_range,
        project=wsclean_options_dict.get("flint_name_suffix"),
    )

    # Now resolve the directory part
    hold_dir = wsclean_options_dict.get("flint_hold_dir") or wsclean_options_dict.get(
        "temp_dir"
    )
    name_dir: Path | str | None = (
        # attempt to resolve possible environment variables flexibly
        parse_environment_variables(variable=str(hold_dir))
        if hold_dir
        else name_ms.path.parent
    )

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
    options_to_comma_join = ("multiscale-scales", "channel-division-frequencies")
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
    ms_list: list[MS],
    wsclean_options: WSCleanOptions,
) -> WSCleanResult:
    """Create a wsclean command from a WSCleanOptions container

    For the most part these are one-to-one mappings to the wsclean CLI with the
    exceptions being:
    #. the `-name` argument will be generated and supplied to the CLI string and will default to the parent directory and name of the supplied measurement set
    #. If `wsclean_options.flint_hold_dir` or `wsclean_options.temp_dir` is specified this directory is used in place of the measurement sets parent directory

    If `container` is supplied to immediately execute this command then the
    output wsclean image products will be moved from the hold directory to the
    same directory as the measurement set.

    Args:
        ms_list (list[MS]): The measurement sets to be imaged
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

    if isinstance(ms_list, MS):
        ms_list = standardise_ms_to_list_ms(ms=ms_list)
        import warnings

        warnings.warn("Input `ms` will need to be list[MS] type.", DeprecationWarning)

    # Some options should also extend the singularity bind directories
    bind_dir_paths = []

    wsclean_options_dict = wsclean_options._asdict()

    example_ms: MS = ms_list[0]
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options, ms=example_ms
    )
    move_directory = example_ms.path.parent
    hold_directory = Path(name_argument_path).parent
    bind_dir_paths.append(hold_directory)

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

    assert isinstance(ms_list, list), f"Expected MSs, got {type(ms_list)}"
    cmds += [f"{ms.path!s}" for ms in ms_list]
    bind_dir_paths += [ms.path.parent for ms in ms_list]

    # TODO: Currently there are two calls into the `parse_environment_variable`
    # when processing the `-temp-dir`/`flint_hold_dir` and `-name` options. When using the `FLINT_UUID`
    # option two separate UUIDs are being used. This is a dirty hack to see if
    # things work. Captains and their ships need better.`
    for bind_dir_path in bind_dir_paths:
        if not Path(bind_dir_path).exists():
            logger.info(f"Creating {bind_dir_path=}")
            Path(bind_dir_path).mkdir(parents=True, exist_ok=True)

    cmd = "wsclean " + " ".join(cmds)

    logger.info(f"Constructed wsclean command: {cmd=}")
    logger.info("Setting default model data column to 'MODEL_DATA'")

    ms_list = [ms.with_options(model_column="MODEL_DATA") for ms in ms_list]

    return WSCleanResult(
        cmd=cmd,
        options=wsclean_options,
        ms=ms_list[0],
        ms_list=ms_list,
        bind_dirs=tuple(bind_dir_paths),
        move_hold_directories=(move_directory, hold_directory),
        image_prefix_str=str(name_argument_path),
    )


def parse_pols(pol_str: str) -> tuple[str, ...] | None:
    """The polarisations wsclean tags each image with, None when a single one is imaged

    Args:
        pol_str (str): The wsclean ``-pol`` value, e.g. ``i``, ``qu`` or ``xx,yy``

    Raises:
        NotSupportedError: ``pol_str`` has an empty or unknown part, or names a feed product other than ``xx,yy``

    Returns:
        tuple[str, ...] | None: The polarisations, in the order they are imaged
    """
    parts = pol_str.lower().split(",")
    tokens = re.findall(r"xx|xy|yx|yy|rr|rl|lr|ll|i|q|u|v", pol_str.lower())
    if not all(parts) or "".join(tokens) != "".join(parts):
        msg = f"Unrecognised polarisation in {pol_str=}"
        raise NotSupportedError(msg)

    pols = tuple(token.upper() for token in tokens)
    # Stokes I is formed from XX and YY, and no other feed product is handled
    if any(len(pol) == 2 for pol in pols) and pols != ("XX", "YY"):
        msg = f"Found instrumental polarisation in {pol_str=}. Only xx,yy is supported."
        raise NotSupportedError(msg)

    return pols if len(pols) > 1 else None


def collect_wsclean_outputs(
    wsclean_result: WSCleanResult,
    prefix: str,
    make_cube_from_subbands: bool = True,
    fitscube_options: FitsCubeOptions | None = None,
    extra_output_types: tuple[str, ...] | None = None,
) -> ImageSet:
    """Gather the images wsclean wrote into an ``ImageSet``, renaming them on disk
    to flint's naming and optionally combining their channels into cubes.

    When ``pol`` is ``xx,yy`` the XX and YY images are replaced by Stokes I. Every
    XX and YY image is then deleted, including those not gathered such as the MFS
    model and dirty images, unless ``flint_keep_linear_images`` is set.

    Args:
        wsclean_result (WSCleanResult): The wsclean command that wrote the images
        prefix (str): The ``-name`` the images were written under, in the directory they are now in
        make_cube_from_subbands (bool, optional): Combine the channel images of each type into a cube. Ignored when more than one polarisation is imaged, whose channel images are left to be cubed per Stokes later. Defaults to True.
        fitscube_options (FitsCubeOptions | None, optional): Options for forming the cubes. Defaults to None.
        extra_output_types (tuple[str, ...] | None, optional): Image types beyond ``image`` and ``residual`` to include, e.g. ``("model",)``. Defaults to None.

    Raises:
        FileNotFoundError: wsclean did not write every image expected of it

    Returns:
        ImageSet: The renamed images
    """
    options = wsclean_result.options
    pols = parse_pols(pol_str=options.pol)

    outputs = find_wsclean_outputs(
        prefix=prefix, channels_out=options.channels_out, pols=pols
    )
    verify_wsclean_outputs(
        outputs=outputs, prefix=prefix, channels_out=options.channels_out, pols=pols
    )

    image_types = ("image", "residual", *(extra_output_types or ()))
    collected = [output for output in outputs if output.image_type in image_types]
    pol_token = pol_name_token(pol=options.pol)
    images: dict[str, list[Path]] = {image_type: [] for image_type in image_types}
    renamed: dict[WSCleanOutput, Path] = {}
    for output in collected:
        flint_path = flint_output_path(
            output=output, prefix=prefix, pol_token=pol_token
        )
        logger.info(f"Renaming {output.path} to {flint_path}")
        output.path.rename(flint_path)
        images[output.image_type].append(flint_path)
        renamed[output] = flint_path

    image_set = ImageSet(prefix=prefix, **images)

    # The XX and YY channels must become Stokes I before cubing, else they would
    # share one cube
    if pols == ("XX", "YY"):
        image_set = form_stokes_i(image_set=image_set)
        if not options.flint_keep_linear_images:
            linear = [
                renamed.get(output, output.path)
                for output in outputs
                if output.pol is not None
            ]
            logger.info(f"Removing {len(linear)} XX and YY images")
            remove_files_folders(*linear)

    if options.save_source_list:
        logger.info("Attaching the wsclean clean components SEDs")
        source_list_path = get_wsclean_output_source_list_path(
            name_path=prefix, pol=None
        )
        assert source_list_path.exists(), f"{source_list_path=} does not exist"
        image_set = image_set.with_options(source_list=source_list_path)

    # One cube can not hold several polarisations, so as the polarisation flow
    # does their channel images are kept, to be cubed per Stokes later
    if make_cube_from_subbands and pols not in (None, ("XX", "YY")):
        logger.warning(
            f"Not forming cubes, as {pols=} are imaged. Their channel images are kept."
        )
        make_cube_from_subbands = False

    if make_cube_from_subbands:
        image_set = combine_image_set_to_cube(
            image_set=image_set,
            fitscube_options=fitscube_options or FitsCubeOptions(),
        )

    return image_set


def run_wsclean_imager(
    wsclean_result: WSCleanResult,
    container: Path,
    make_cube_from_subbands: bool = True,
    fitscube_options: FitsCubeOptions | None = None,
    extra_output_types: tuple[str, ...] | None = None,
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
        extra_output_types (tuple[str, ...] | None, optional): Additional output types (beyond ``image``/``residual``) to search for and attach to the returned ``ImageSet``, e.g. ``("model",)``. Defaults to None.

    Returns:
        ImageSet: The executed wsclean output products.
    """

    ms = wsclean_result.ms
    wsclean_cleanup = wsclean_result.cleanup
    bind_dirs = wsclean_result.bind_dirs
    move_hold_directories = wsclean_result.move_hold_directories
    image_prefix_str = wsclean_result.image_prefix_str

    if isinstance(ms, MS):
        sclient_bind_dirs = [Path(ms.path).parent.absolute()]
    else:
        sclient_bind_dirs = [Path(_ms.path).parent.absolute() for _ms in ms.mss]
    if bind_dirs:
        sclient_bind_dirs = sclient_bind_dirs + list(bind_dirs)

    prefix = image_prefix_str if image_prefix_str else None
    if prefix is None:
        prefix = (
            str(ms.path.parent / ms.path.name)
            if isinstance(ms, MS)
            else str(ms.mss[0].path.parent / ms.path.name)
        )
        logger.warning(f"Setting prefix to {prefix}. Likely this is not correct. ")

    channels_out = wsclean_result.options.channels_out
    pols = parse_pols(pol_str=wsclean_result.options.pol)

    # Clear what an earlier attempt left under this name, so every image found
    # after the run is from it. A task rerun after a dask worker death meets its
    # own partial outputs, so they are removed rather than refused.
    output_directories = {Path(prefix).parent}
    if move_hold_directories:
        output_directories.add(Path(move_hold_directories[0]))
    for directory in output_directories:
        clear_wsclean_outputs(
            prefix=directory / Path(prefix).name, channels_out=channels_out, pols=pols
        )

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
                rm_files = remove_wsclean_outputs(
                    outputs=find_wsclean_outputs(
                        prefix=prefix, channels_out=channels_out, pols=pols
                    )
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
        remove_wsclean_outputs(
            outputs=find_wsclean_outputs(
                prefix=prefix, channels_out=channels_out, pols=pols
            )
        )

    image_set = collect_wsclean_outputs(
        wsclean_result=wsclean_result,
        prefix=prefix,
        make_cube_from_subbands=make_cube_from_subbands,
        fitscube_options=fitscube_options,
        extra_output_types=extra_output_types,
    )

    logger.info(f"Constructed {image_set=}")

    return image_set


def wsclean_imager(
    ms: Path | MS | tuple[MS | Path, ...] | list[MS | Path],
    wsclean_container: Path,
    update_wsclean_options: dict[str, Any] | None = None,
    update_fitscube_options: dict[str, Any] | None = None,
    make_cube_from_subbands: bool = True,
    extra_output_types: tuple[str, ...] | None = None,
) -> WSCleanResult:
    """Create and run a wsclean imager command against a measurement set.

    Args:
        ms (Path | MS | tuple[MS | Path, ...] | list[MS | Path]): Path to the measurement set that will be imaged
        wsclean_container (Path): Path to the container with wsclean installed
        update_wsclean_options (Optional[Dict[str, Any]], optional): Additional options to update the generated WscleanOptions with. Keys should be attributes of WscleanOptions. Defaults to None.

    Returns:
        WSCleanResult: _description_
    """

    ms_list: list[MS] = standardise_ms_to_list_ms(ms=ms)
    assert isinstance(ms, list), f"Should be list, got {ms}"
    assert all(isinstance(_ms, MS) for _ms in ms_list)

    # Help out the linter
    del ms

    wsclean_options = WSCleanOptions()
    if update_wsclean_options:
        logger.info("Updatting wsclean options with user-provided items. ")
        wsclean_options = wsclean_options.with_options(**update_wsclean_options)

    fitscube_options = FitsCubeOptions()
    if update_fitscube_options:
        logger.info("Updatting fitscube options with user-provided items. ")
        fitscube_options = fitscube_options.with_options(**update_fitscube_options)

    for field in ("temp_dir", "flint_hold_dir"):
        value = getattr(wsclean_options, field)
        if isinstance(value, str):
            resolved = parse_environment_variables(value)
            logger.info(f"Resolved {field} to {resolved}")
            wsclean_options = wsclean_options.with_options(**{field: resolved})

    assert ms_list[0].column is not None, (
        "A MS column needs to be elected for imaging. "
    )
    wsclean_options = wsclean_options.with_options(data_column=ms_list[0].column)
    wsclean_result = create_wsclean_cmd(
        ms_list=ms_list,
        wsclean_options=wsclean_options,
    )
    image_set = run_wsclean_imager(
        wsclean_result=wsclean_result,
        container=wsclean_container,
        fitscube_options=fitscube_options,
        make_cube_from_subbands=make_cube_from_subbands,
        extra_output_types=extra_output_types,
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
    # The wsclean options class has a verbose option. Since we can not
    # change that we are simply using the shortform option only.
    # TODO: Could consider using something like 'debug' here, ya sea dog
    wsclean_parser.add_argument("-v", action="store_true", help="Extra output logging.")
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
        if args.v:
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
