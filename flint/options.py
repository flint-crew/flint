"""Contains the core of the option class containers that are used to
hold stateful properties throughout the flint codebase.
"""

# NOTE: Although these options could be stored closer to where
# their logic is often used, at times these can cause circular dependencies.
# This happens a lot with the linting / typing checking, where classes are
# imported purely for tools like ruff

from __future__ import (  # Used for mypy/pylance to like the return type of MS.with_options
    annotations,
)

from argparse import ArgumentParser, Namespace
from pathlib import Path
from types import NoneType, UnionType
from typing import (
    Any,
    NamedTuple,
    TypeVar,
    get_args,
    get_origin,
)

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic.fields import FieldInfo

from flint.exceptions import MSError
from flint.logging import logger


class MS(NamedTuple):
    """Helper to keep track of measurement set information

    This is the class that should be used when describing a measurement
    set that will be operated on.
    """

    path: Path
    """Path to the measurement set that is being represented"""
    column: str | None = None
    """Column that should be operated against"""
    beam: int | None = None
    """The beam ID of the MS within an ASKAP field"""
    spw: int | None = None
    """Intended to be used with ASKAP high-frequency resolution modes, where the MS is divided into SPWs"""
    field: str | None = None
    """The field name  of the data"""
    model_column: str | None = None
    """The column name of the most recently MODEL data"""

    @property
    def ms(self) -> MS:
        return self

    @classmethod
    def cast(cls, ms: MS | Path) -> MS:
        """Create/return a MS instance given either a Path or MS.

        If the input is neither a MS instance or Path, the object will
        be checked to see if it has a `.ms` attribute. If it does then
        this will be used.

        Args:
            ms (Union[MS, Path]): The input type to consider

        Raises:
            MSError: Raised when the input ms can not be cast to an MS instance

        Returns:
            MS: A normalised MS
        """
        if isinstance(ms, MS):
            # Nothing to do
            pass
        elif isinstance(ms, Path):
            ms = MS(path=ms)
        elif "ms" in dir(ms) and isinstance(ms.ms, MS):
            ms = ms.ms
        else:
            raise MSError(f"Unable to convert {ms=} of {type(ms)} to MS object. ")

        return ms

    def with_options(self, **kwargs) -> MS:
        """Create a new MS instance with keywords updated

        Returns:
            MS: New MS instance with updated attributes
        """
        # TODO: Update the signature to have the actual attributes to
        # help keep mypy and other linters happy
        as_dict = self._asdict()
        as_dict.update(kwargs)

        return MS(**as_dict)


def options_to_dict(input_options: Any) -> dict:
    """Helper function to convert an `Options` type class to a dictionary.

    Most of `flint` `Option` and `Result` classes used `typing.NamedTuples`, which carry with
    it a `_asdict` method to convert them to a dictionary. Future roadmap plans to move over to
    pydantic type models. This is a place holder function to help transition to this.

    Args:
        input_options (Any): Item to convert to a dictionary

    Raises:
        TypeError: Raised if the conversion to a dictionary was not successful

    Returns:
        Dict: The dictionary version of the input options
    """

    if "_asdict" in dir(input_options):
        return input_options._asdict()

    try:
        if issubclass(input_options, BaseModel):
            return dict(**input_options.__dict__)
    except TypeError:
        logger.debug(f"can not use issubclass on {input_options}")

    try:
        return dict(**input_options)
    except TypeError:
        raise TypeError(f"Input options is not known: {type(input_options)}")


T = TypeVar("T", bound=BaseModel)


class BaseOptions(BaseModel):
    """A base class that Options style flint classes can
    inherit from. This is derived from ``pydantic.BaseModel``,
    and can be used for validation of supplied values.

    Class derived from ``BaseOptions`` are immutable by
    default, and have the docstrings of attributes
    extracted.
    """

    model_config = ConfigDict(
        frozen=True, from_attributes=True, use_attribute_docstrings=True, extra="forbid"
    )

    def with_options(self: T, /, **kwargs) -> T:
        new_args = self.__dict__.copy()
        new_args.update(**kwargs)

        return self.__class__(**new_args)

    def _asdict(self) -> dict[str, Any]:
        return self.__dict__


def _create_argparse_options(name: str, field: FieldInfo) -> tuple[str, dict[str, Any]]:
    """Convert a pydantic Field into ``dict`` to splate into ArgumentParser.add_argument()"""

    field_name = name if field.is_required() else "--" + name.replace("_", "-")

    field_type = get_origin(field.annotation)
    field_args = get_args(field.annotation)
    iterable_types = (list, tuple, set)

    options = dict(action="store", help=field.description, default=field.default)

    if field.annotation is bool:
        options["action"] = "store_false" if field.default else "store_true"

    # if field_type is in (list, tuple, set) OR if (list, tuple, set) | Any
    elif field_type in iterable_types or (
        field_type is UnionType
        and any(get_origin(p) in iterable_types for p in field_args)
    ):
        nargs: str | int = "+"

        # If the field is a tuple, and the Ellipsis is not present
        # We can assume that the nargs is the length of the tuple
        if field_type is tuple and Ellipsis not in field_args:
            nargs = len(field_args)

        # Now we handle unions, but do the same check as above
        elif field_type is UnionType and Ellipsis not in field_args:
            for arg in field_args:
                args = get_args(arg)
                if arg is not NoneType and type(args) is tuple and Ellipsis not in args:
                    nargs = len(args)

        if nargs == 0:
            raise ValueError(f"Unable to determine nargs for {name=}, got {nargs=}")
        options["nargs"] = nargs

    return field_name, options


def add_options_to_parser(
    parser: ArgumentParser,
    options_class: type[BaseOptions],
    description: str | None = None,
) -> ArgumentParser:
    """Given an established argument parser and a class derived
    from a ``pydantic.BaseModel``, populate the argument parser
    with the model properties.

    Args:
        parser (ArgumentParser): Parser that arguments will be added to
        options_class (type[BaseModel]): A ``Options`` style class derived from ``BaseOptions``

    Returns:
        ArgumentParser: Updated argument parser
    """

    assert issubclass(options_class, BaseModel), (
        f"{options_class=} is not a pydantic BaseModel"
    )

    group = parser.add_argument_group(
        title=f"Inputs for {options_class.__name__}", description=description
    )

    for name, field in options_class.model_fields.items():
        field_name, options = _create_argparse_options(name=name, field=field)
        try:
            group.add_argument(field_name, **options)  # type: ignore
        except Exception as e:
            logger.error(f"{field_name=} {options=}")
            raise e

    return parser


U = TypeVar("U", bound=BaseOptions)


def create_options_from_parser(
    parser_namespace: Namespace, options_class: type[U]
) -> U:
    """Given a ``BaseOptions`` derived class, extract the corresponding
    arguments from an ``argparse.nNamespace``. These options correspond to
    ones generated by ``add_options_to_parser``.

    Args:
        parser_namespace (Namespace): The argument parser corresponding to those in the ``BaseOptions`` class
        options_class (U): A ``BaseOptions`` derived class

    Returns:
        U: An populated options class with arguments drawn from CLI argument parser
    """
    assert issubclass(
        options_class,  # type: ignore
        BaseModel,
    ), f"{options_class=} is not a pydantic BaseModel"

    args = (
        vars(parser_namespace)
        if not isinstance(parser_namespace, dict)
        else parser_namespace
    )

    opts_dict = {}
    for name, field in options_class.model_fields.items():
        opts_dict[name] = args[name]

    return options_class(**opts_dict)


class BandpassOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout the processing of bandpass calibration
    data.

    In its present form this `BandpassOptions` class is not intended
    to contain properties of the data that arebeing processed, rather
    how these data will be processed.

    These settings are not meant to be adjustabled throughout
    a single bandpass pipeline run
    """

    flagger_container: Path | None = None
    """Path to the singularity aoflagger container"""
    calibrate_container: Path | None = None
    """Path to the singularity calibrate container"""
    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    smooth_solutions: bool = False
    """Will activate the smoothing of the bandpass solutions"""
    smooth_window_size: int = 16
    """The width of the smoothing window used to smooth the bandpass solutions"""
    smooth_polynomial_order: int = 4
    """The polynomial order used by the Savgol filter when smoothing the bandpass solutions"""
    flag_calibrate_rounds: int = 3
    """The number of times the bandpass will be calibrated, flagged, then recalibrated"""
    minuv: float | None = None
    """The minimum baseline length, in meters, for data to be included in bandpass calibration stage"""
    preflagger_ant_mean_tolerance: float = 0.2
    """Tolerance that the mean x/y antenna gain ratio test before the antenna is flagged"""
    preflagger_mesh_ant_flags: bool = False
    """Share channel flags from bandpass solutions between all antenna"""
    preflagger_jones_max_amplitude: float | None = None
    """Flag Jones matrix if any amplitudes with a Jones are above this value"""


class AddModelSubtractFieldOptions(BaseOptions):
    """Options related to predicting a continuum model during the SubtractFieldOptions workflow.
    Specifically these options deal with identifying the wsclean produced source list model, which
    may be used by ``admodel`` to predict model visibilities. See utilities around the ``aocalibrate``
    functions and routines."""

    wsclean_pol_mode: list[str] = ["i"]
    """The polarisation of the wsclean model that was generated"""
    calibrate_container: Path | None = None
    """Path to the container with the calibrate software (including addmodel)"""
    addmodel_cluster_config: Path | None = None
    """Specify a new cluster configuration file different to the preferred on. If None, drawn from preferred cluster config"""


class SubtractFieldOptions(BaseOptions):
    """Container for options related to the
    continuum-subtracted pipeline"""

    wsclean_container: Path
    """Path to the container with wsclean"""
    yandasoft_container: Path
    """Path to the container with yandasoft"""
    subtract_model_data: bool = False
    """Subtract the MODEL_DATA column from the nominated data column"""
    data_column: str = "CORRECTED_DATA"
    """Describe the column that should be imaed and, if requested, have model subtracted from"""
    expected_ms: int = 36
    """The number of measurement sets that should exist"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    linmos_residuals: bool = False
    """Linmos the cleaning residuals together into a field image"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    stagger_delay_seconds: float | None = None
    """The delay, in seconds, that should be used when submitting items in batches (e.g. looping over channels)"""
    attempt_subtract: bool = False
    """Attempt to subtract the model column from the nominated data column"""
    subtract_data_column: str = "DATA"
    """Should the continuum model be subtracted, where to store the output"""
    predict_wsclean_model: bool = False
    """Search for the continuum model produced by wsclean and subtract"""
    use_addmodel: bool = False
    """Invoke the ``addmodel`` visibility prediction, including the search for the ``wsclean`` source list"""
    use_crystalball: bool = False
    """Attempt to predict the model visibilities using ``crystalball``"""


class FieldOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout components related to the actual
    pipeline.

    In its present form this `FieldOptions` class is not intended
    to contain properties of the data that are being processed,
    rather how those data will be processed.

    These settings are not meant to be adjustable throughout
    rounds of self-calibration.
    """

    flagger_container: Path | None = None
    """Path to the singularity aoflagger container"""
    calibrate_container: Path | None = None
    """Path to the singularity calibrate container"""
    casa_container: Path | None = None
    """Path to the singularity CASA container"""
    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    wsclean_container: Path | None = None
    """Path to the singularity wsclean container"""
    yandasoft_container: Path | None = None
    """Path to the singularity yandasoft container"""
    potato_container: Path | None = None
    """Path to the singularity potato peel container"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    rounds: int = 2
    """Number of required rouds of self-calibration and imaging to perform"""
    skip_selfcal_on_rounds: list[int] | None = None
    """Do not perform the derive and apply self-calibration solutions on these rounds"""
    zip_ms: bool = False
    """Whether to zip measurement sets once they are no longer required"""
    run_aegean: bool = False
    """Whether to run the aegean source finding tool"""
    aegean_container: Path | None = None
    """Path to the singularity aegean container"""
    no_imaging: bool = False
    """Whether to skip the imaging process (including self-calibration)"""
    reference_catalogue_directory: Path | None = None
    """Path to the directory container the reference catalogues, used to generate validation plots"""
    linmos_residuals: bool = False
    """Linmos the cleaning residuals together into a field image"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    fixed_beam_shape: tuple[float, float, float] | None = None
    """Specify the final beamsize of linmos field images in (arcsec, arcsec, deg)"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    use_preflagger: bool = False
    """Whether to apply (or search for solutions with) bandpass solutions that have gone through the preflagging operations"""
    use_smoothed: bool = False
    """Whether to apply (or search for solutions with) a bandpass smoothing operation applied"""
    use_beam_masks: bool = False
    """Construct beam masks from MFS images to use for the next round of imaging. """
    use_beam_masks_from: int = 1
    """If `use_beam_masks` is True, this sets the round where beam masks will be generated from"""
    use_beam_masks_rounds: list[int] | None = None
    """If `use_beam_masks` is True, this sets which rounds should have a mask applied"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    sbid_archive_path: Path | None = None
    """Path that SBID archive tarballs will be created under. If None no archive tarballs are created. See ArchiveOptions. """
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """
    rename_ms: bool = False
    """Rename MSs throughout rounds of imaging and self-cal instead of creating copies. This will delete data-columns throughout. """
    stokes_v_imaging: bool = False
    """Specifies whether Stokes-V imaging will be carried out after the final round of imagine (whether or not self-calibration is enabled). """
    coadd_cubes: bool = False
    """Co-add cubes formed throughout imaging together. Cubes will be smoothed channel-wise to a common resolution. Only performed on final set of images"""
    update_model_data_with_source_list: bool = False
    """Attempt to update a MSs MODEL_DATA column with a source list (e.g. source list output from wsclean)"""


class PolFieldOptions(BaseOptions):
    """Container that represents the flint related options that
    might be used throughout components related to the actual
    pipeline.

    In its present form this `PolFieldOptions` class is not intended
    to contain properties of the data that are being processed,
    rather how those data will be processed.

    These settings are not meant to be adjustable across different polarisations.
    """

    expected_ms: int = 36
    """The expected number of measurement set files to find"""
    wsclean_container: Path | None = None
    """Path to the singularity wsclean container"""
    yandasoft_container: Path | None = None
    """Path to the singularity yandasoft container"""
    casa_container: Path | None = None
    """Path to the singularity CASA container"""
    holofile: Path | None = None
    """Path to the holography FITS cube that will be used when co-adding beams"""
    beam_cutoff: float = 150
    """Cutoff in arcseconds to use when calculating the common beam to convol to"""
    fixed_beam_shape: tuple[float, float, float] | None = None
    """Specify the final beamsize of linmos field images in (arcsec, arcsec, deg)"""
    pb_cutoff: float = 0.1
    """Primary beam attenuation cutoff to use during linmos"""
    trim_linmos_fits: bool = False
    """Trim the linmos fits files to remove the padding that is added. If True, the output fits files will be smaller but might be different shapes"""
    imaging_strategy: Path | None = None
    """Path to a FLINT imaging yaml file that contains settings to use throughout imaging"""
    sbid_copy_path: Path | None = None
    """Path that final processed products will be copied into. If None no copying of file products is performed. See ArchiveOptions. """


def dump_field_options_to_yaml(
    output_path: Path,
    field_options: FieldOptions | PolFieldOptions | SubtractFieldOptions,
    overwrite: bool = False,
) -> Path:
    """Dump the supplied instance of `FieldOptions` to a yaml file
    for record keeping.

    The parent directory of the `output_path` will be created if it
    does not already exist.

    Args:
        output_path (Path): Path of the output file.
        field_options (FieldOptions): The `FieldOptions` class to write.
        overwrite (bool, optional): Overwrite the file if it exists. Defaults to False.

    Raises:
        FileExistsError: Raise if `output_path` already exists and `overwrite` is `False`

    Returns:
        Path: Output path written to.
    """

    logger.info(f"Writing field_options to {output_path}")

    if not overwrite and output_path.exists():
        raise FileExistsError(f"{output_path=} exists. ")

    # Create the directory just in case
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as out_file:
        yaml.dump(data=field_options._asdict(), stream=out_file, sort_keys=False)

    return output_path


# TODO: Perhaps move these to flint.naming, and can be built up
# based on rules, e.g. imager used, source finder etc.
DEFAULT_TAR_RE_PATTERNS = (
    r".*MFS.*image\.fits",
    r".*linmos.*",
    r".*weight\.fits",
    r".*yaml",
    r".*\.txt",
    r".*png",
    r".*beam[0-9]+\.ms\.zip",
    r".*beam[0-9]+\.ms",
    r".*\.caltable",
    r".*\.tar",
    r".*\.csv",
)
DEFAULT_COPY_RE_PATTERNS = (r".*linmos.*fits", r".*weight\.fits", r".*png", r".*csv")


class ArchiveOptions(BaseOptions):
    """Container for options related to archiving products from flint workflows"""

    tar_file_re_patterns: tuple[str, ...] = DEFAULT_TAR_RE_PATTERNS
    """Regular-expressions to use to collect files that should be tarballed"""
    copy_file_re_patterns: tuple[str, ...] = DEFAULT_COPY_RE_PATTERNS
    """Regular-expressions used to identify files to copy into a final location (not tarred)"""
