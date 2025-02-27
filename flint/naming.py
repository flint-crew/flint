"""Attempts to centralise components to do with naming of pipeline files and data
products.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeVar

from flint.exceptions import NamingException
from flint.logging import logger

PathStr = TypeVar("PathStr", str, Path)


def _rename_linear_to_stokes(
    linear_name_str: str,
    stokes: str,
) -> str:
    if stokes.lower() not in ("q", "u"):
        raise NameError(f"Stokes {stokes=} is not linear!")
    pattern = r"\.qu"  # Regex pattern to replace
    stokes_name = re.sub(pattern, f".{stokes}", linear_name_str)
    logger.info(f"Renamed {linear_name_str=} to {stokes_name=}")
    return stokes_name


def rename_linear_to_stokes(
    linear_name: PathStr,
    stokes: str,
) -> PathStr:
    if isinstance(linear_name, Path):
        return Path(_rename_linear_to_stokes(linear_name.as_posix(), stokes))

    return _rename_linear_to_stokes(linear_name, stokes)


def get_fits_cube_from_paths(paths: list[Path]) -> list[Path]:
    """Given a list of files, find the ones that appear to be FITS files
    and contain the ``.cube.`` field indicator. A regular expression searching
    for both the ``.cube.`` and ``.fits`` file type is used.

    Args:
        paths (List[Path]): The set of paths to examine to identify potential cube fits images from

    Returns:
        List[Path]: Set of paths matching the search criteria
    """
    cube_expression = re.compile(r"\.cube\..*fits$")

    cube_files = [path for path in paths if bool(cube_expression.search(str(path)))]

    return cube_files


LONG_FIELD_TO_SHORTHAND = {
    "sbid": "SB",
    "beam": "beam",
    "channel_range": "ch",
    "round": "round",
}
"""Name mapping between the longform of ProcessedFieldComponents and shorthands used"""


def create_name_from_common_fields(
    in_paths: tuple[Path, ...], additional_suffixes: str | None = None
) -> Path:
    """Attempt to craft a base name using the field elements that are in common.
    The expectation that these are paths that can be processed by the ``processed_name_format``
    handler. Resulting fields that are common across all ``in_paths`` are preserved.

    Only fields that are recognised as a known property are retained. Suffixes that do not
    form a component are ignored. For example:

    >>> "59058/SB59058.RACS_1626-84.ch0287-0288.linmos.fits"

    the `linmos.fits` would be ignored.

    All ``in_paths`` should be detected, otherwise an ValueError is raised

    Args:
        in_paths (Tuple[Path, ...]): Collection of input paths to consider
        additional_suffixes (Optional[str], optional): Add an additional set of suffixes before returning. Defaults to None.

    Raises:
        ValueError: Raised if any of the ``in_paths`` fail to conform to ``flint`` processed name format

    Returns:
        Path: Common fields with the same base parent path
    """
    from flint.options import options_to_dict

    in_paths = tuple(Path(p) for p in in_paths)
    parent = in_paths[0].parent
    processed_components = list(map(processed_ms_format, in_paths))

    if None in processed_components:
        raise ValueError("Processed name format failed")
    processed_components_dict = [options_to_dict(pc) for pc in processed_components]

    keys_to_test = processed_components_dict[0].keys()
    logger.info(f"{keys_to_test=}")
    # Extract the fields that are constant across all inputs and are not None
    constant_fields = {
        key: processed_components_dict[0][key]
        for key in keys_to_test
        if len(set([pcd[key] for pcd in processed_components_dict])) == 1
        and processed_components_dict[0][key] is not None
    }

    name_path = create_path_from_processed_name_components(
        processed_name_components=ProcessedNameComponents(**constant_fields),
        parent_path=parent,
    )
    constant_field_keys = list(constant_fields.keys())
    logger.info(f"Identified {constant_field_keys=}")

    if additional_suffixes:
        additional_suffixes = (
            f".{additional_suffixes}"
            if not additional_suffixes.startswith(".")
            else additional_suffixes
        )
        name_path = Path(str(name_path) + additional_suffixes)
    return Path(name_path)


# TODO: Need to assess the mode argument, and define literals that are accepted
def create_image_cube_name(
    image_prefix: Path,
    mode: str | list[str] | None = None,
    suffix: str | list[str] | None = None,
) -> Path:
    """Create a consistent naming scheme when combining images into cube images. Intended to
    be used when combining many subband images together into a single cube.

    The name returned will be:
    >>> {image_prefix}.{mode}.{suffix}.cube.fits

    Should ``mode`` or ``suffix`` be a list, they will be joined with '.' separators. Hence, no
    '.' should be added.

    This function will always output 'cube.fits' at the end of the returned file name.

    Args:
        image_prefix (Path): The unique path of the name. Generally this is the common part among the input planes
        mode (Optional[Union[str, List[str]]], optional): Additional mode/s to add to the file name. Defaults to None.
        suffix (Optional[Union[str, List[str]]], optional): Additional suffix/s to add before the final 'cube.fits'. Defaults to None.

    Returns:
        Path: The final path and file name
    """
    # NOTE: This is likely a function to grow in time as more imaging and pipeline modes added. Putting
    # it here for future proofing
    output_cube_name = f"{Path(image_prefix)!s}.{mode}.{suffix}"

    output_components = [str(Path(image_prefix))]
    if mode:
        # TODO: Assess what modes are actually allowed. Suggestion is to
        # make a class of some sort with specified and known markers that
        # are opted into. Hate this "everything and anything"
        (
            output_components.append(mode)
            if isinstance(mode, str)
            else output_components.extend(mode)
        )
    if suffix:
        # TODO: See above. Need a class of acceptable suffixes to use
        (
            output_components.append(suffix)
            if isinstance(suffix, str)
            else output_components.extend(suffix)
        )

    output_components.append("cube.fits")

    output_cube_name = ".".join(output_components)
    return Path(output_cube_name)


def create_imaging_name_prefix(
    ms_path: Path,
    pol: str | None = None,
    channel_range: tuple[int, int] | None = None,
) -> str:
    """Given a measurement set and a polarisation, create the naming prefix to be used
    by some imager

    Args:
        ms (Union[MS,Path]): The measurement set being considered
        pol (Optional[str], optional): Whether a polarsation is being considered. Defaults to None.
        channel_range (Optional[Tuple[int,int]], optional): The channel range that is going to be imaged. Defaults to none.

    Returns:
        str: The constructed string name
    """

    names = [ms_path.stem]
    if pol:
        names.append(f"{pol.lower()}")
    if channel_range:
        names.append(f"ch{channel_range[0]:04}-{channel_range[1]:04}")

    return ".".join(names)


ResolutionModes = Literal["optimal", "fixed"]


def get_beam_resolution_str(mode: ResolutionModes, marker: str | None = None) -> str:
    """Map a beam resolution mode to an appropriate suffix. This
    is located her in anticipation of other imaging modes.

    Supported modes are: 'optimal', 'fixed', 'raw'

    Args:
        mode (Literal["fixed","optimal"]): The mode of image resolution to use.
        marker (Optional[str], optional): Append the marker to the end of the returned mode string. If None mode string is returned. Defaults to None.

    Raises:
        ValueError: Raised when an unrecognised mode is supplied

    Returns:
        str: The appropriate string for mapped mode
    """
    # NOTE: Arguably this is a trash and needless function. Adding it
    # in case other modes are ever needed or referenced. No idea whether
    # it will ever been needed and could be removed in future.
    supported_modes: dict[str, str] = dict(optimal="optimal", fixed="fixed", raw="raw")
    if mode.lower() not in supported_modes.keys():
        raise ValueError(
            f"Received {mode=}, supported modes are {supported_modes.keys()}"
        )

    mode_str = supported_modes[mode.lower()]

    return mode_str + marker if marker else mode_str


def update_beam_resolution_field_in_path(
    path: Path,
    original_mode: ResolutionModes,
    updated_mode: ResolutionModes,
    marker: str | None = None,
) -> Path:
    """Transition the resolution indicator in a processed name (either ``optimal`` or ``fixed``)
    to another state. For example:

    >>> 'SB57516.RACS_0929-81.round4.i.optimal.round4.residual.linmos.fits'

    to

    >>> 'SB57516.RACS_0929-81.round4.i.fixed.round4.residual.linmos.fits'

    See ``get_beam_resolution_str`` for addition information. Supported modes are
    ``fixed`` and ``optimal``

    Args:
        path (Path): The path to inspect and update
        original_mode (ResolutionModes): The original mode
        updated_mode (ResolutionModes): The mode to move to
        marker (str | None, optional): The marker to separate the field. Defaults to None.

    Returns:
        Path: Updated path
    """
    original_mode_str = get_beam_resolution_str(mode=original_mode, marker=marker)
    updated_mode_str = get_beam_resolution_str(mode=updated_mode, marker=marker)

    assert original_mode_str in str(path), f"{original_mode_str=} not in {path=}"
    new_path = Path(str(path).replace(original_mode_str, updated_mode_str))
    logger.info(
        f"Updated beam resolution mode from {original_mode=} to {updated_mode=}"
    )

    return new_path


def get_selfcal_ms_name(in_ms_path: Path, round: int = 1) -> Path:
    """Create the new output MS path that will be used for self-calibration. The
    output measurement set path will include a roundN.ms suffix, where N is the
    round. If such a suffix already exists from an earlier self-calibration round,
    it will be removed and replaced.

    Args:
        in_ms_path (Path): The measurement set that will go through self-calibration
        round (int, optional): The self-calibration round number that is currently being used. Defaults to 1.

    Returns:
        Path: Output measurement set path to use
    """
    res = re.search("\\.round[0-9]+.ms", str(in_ms_path.name))
    if res:
        logger.info("Detected a previous round of self-calibration. ")
        span = res.span()
        name_str = str(in_ms_path.name)
        name = f"{name_str[: span[0]]}.round{round}.ms"
    else:
        name = f"{in_ms_path.stem!s}.round{round}.ms"
    out_ms_path = in_ms_path.parent / name

    assert in_ms_path != out_ms_path, (
        f"{in_ms_path=} and {out_ms_path=} match. Something went wrong when creating new self-cal name. "
    )

    return out_ms_path


def add_timestamp_to_path(
    input_path: Path | str, timestamp: datetime | None = None
) -> Path:
    """Add a timestamp to a input path, where the timestamp is the
    current data and time. The time will be added to the name component
    before the file suffix. If the name component of the `input_path`
    has multiple suffixes than the timestamp will be added before the last.

    Args:
        input_path (Union[Path, str]): Path that should have a timestamp added
        timestamp: (Optional[datetime], optional): The date-time to add. If None the current time is used. Defaults to None.
    Returns:
        Path: Updated path with a timestamp in the file name
    """
    input_path = Path(input_path)
    timestamp = timestamp if timestamp else datetime.now()

    time_str = timestamp.strftime("%Y%m%d-%H%M%S")
    new_name = f"{input_path.stem}-{time_str}{input_path.suffix}"
    output_path = input_path.with_name(new_name)

    return output_path


class CASDANameComponents(NamedTuple):
    """Container for the components of a CASDA MS. These are really those
    processed by the ASKAP pipeline"""

    sbid: int
    """The sbid of the observation"""
    field: str
    """The name of the field extracted"""
    beam: str
    """Beam number of the data"""
    spw: str | None = None
    """If multiple MS were written as the data were in a high-frequency resolution mode, which segment"""
    alias: str | None = None
    """Older ASKAP MSs could be packed with multiple fields. The ASKAP pipeline holds this field as an alias. They are now the same in almost all cases as the field. """
    format: str = "science"
    """What the format / type of the data the MS is. """


def casda_ms_format(in_name: str | Path) -> CASDANameComponents | None:
    """Break up a CASDA sty;e MS name (really the askap pipeline format) into its recognised parts.
    if a match fails a `None` is returned.

    Example of a CASDA style MS:

    - `scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms`

    Args:
        in_name (Union[str, Path]): The path to or name of the MS to consider

    Returns:
        Union[CASDANameComponents, None]: The returned components of the MS. If this fails a `None` is returned.
    """

    in_name = Path(in_name).name

    # An example
    # scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms

    logger.debug(f"Matching {in_name}")
    regex = re.compile(
        r"^(?P<format>(scienceData|1934))(\.(?P<alias>.*))?\.SB(?P<sbid>[0-9]+)(\.(?P<field>.*))?\.beam(?P<beam>[0-9]+).*ms"
    )
    results = regex.match(in_name)

    if results is None:
        logger.debug(f"No casda_ms_format results to {in_name} found")
        return None

    return CASDANameComponents(
        sbid=int(results["sbid"]),
        field=results["field"],
        beam=results["beam"],
        alias=results["alias"],
        format=results["format"],
    )


class RawNameComponents(NamedTuple):
    date: str
    """Date that the data were taken, of the form YYYY-MM-DD"""
    time: str
    """Time that the data were written"""
    beam: str
    """Beam number of the data"""
    spw: str | None = None
    """If multiple MS were written as the data were in a high-frequency resolution mode, which segment"""


def raw_ms_format(in_name: str) -> None | RawNameComponents:
    """The typical ASKAP measurement written to the ingest disks
    has the form:

    >>> 2022-04-14_100122_1.ms

    and in the case of a multiple beams written out (in high frequency resolution mode)

    >>> 2022-04-14_100122_1_1.ms

    This function will attempt to break it up into its main parts
    and return the mapping.

    Args:
        in_name (str): The name of a file, presumably a measurement set. The left-most part will be examined for to identify the raw ASKAP naming scheme.

    Returns:
        Union[None,Dict[str,str]]: None if the raw ASKAP measurement set naming scheme was not detected, otherwise a dictionary representing its parts.
    """

    logger.debug(f"Matching {in_name}")
    regex = re.compile(
        "^(?P<date>[0-9]{4}-[0-9]{1,2}-[0-9]{1,2})_(?P<time>[0-9]+)_(?P<beam>[0-9]+)(_(?P<spw>[0-9]+))*"
    )
    results = regex.match(in_name)

    if results is None:
        logger.debug(f"No raw_ms_format results to {in_name} found")
        return None

    groups = results.groupdict()

    logger.debug(f"Matched groups are: {groups}")

    return RawNameComponents(
        date=groups["date"], time=groups["time"], beam=groups["beam"], spw=groups["spw"]
    )


class ProcessedNameComponents(NamedTuple):
    """Container for a file name derived from a MS flint name. Generally of the
    form: SB.Field.Beam.Spw"""

    sbid: str
    """The sbid of the observation"""
    field: str
    """The name of the field extracted"""
    beam: str | None = None
    """The beam of the observation processed"""
    spw: str | None = None
    """The SPW of the observation. If there is only one spw this is None."""
    round: str | None = None
    """The self-calibration round detected. This might be represented as 'noselfcal' in some image products, e.g. linmos. """
    pol: str | None = None
    """The polarisation component, if it exists, in a filename. Examples are 'i','q','u','v'. Could be combinations in some cases depending on how it was created (e.g. based on wsclean pol option). """
    channel_range: tuple[int, int] | None = None
    """The channel range encoded in an file name. Generally are zero-padded, and are two fields of the form ch1234-1235, where the upper bound is exclusive. Defaults to none."""


def processed_ms_format(
    in_name: str | Path,
) -> ProcessedNameComponents | None:
    """Will take a formatted name (i.e. one derived from the flint.naming.create_ms_name)
    and attempt to extract its main components. This includes the SBID, field, beam and spw.

    Args:
        in_name (Union[str, Path]): The name that needs to be broken down into components

    Returns:
        Union[FormattedNameComponents,None': A structure container the sbid, field, beam and spw. None is returned if can not be parsed.
    """

    in_name = in_name.name if isinstance(in_name, Path) else in_name

    logger.debug(f"Matching {in_name}")
    # TODO: Should the Beam and field items be relaxed and allowed to be optional?
    # TODOL At very least I think the beam should become options
    # A raw string is used to avoid bad unicode escaping
    regex = re.compile(
        r"^SB(?P<sbid>[0-9]+)"
        r"\.(?P<field>[^.]+)"
        r"((\.beam(?P<beam>[0-9]+))?)"
        r"((\.spw(?P<spw>[0-9]+))?)"
        r"((\.round(?P<round>[0-9]+))?)"
        r"((\.(?P<pol>(i|q|u|v|xx|yy|xy|yx)+))?)"
        r"((\.ch(?P<chl>([0-9]+))-(?P<chh>([0-9]+)))?)"
    )
    results = regex.match(in_name)

    if results is None:
        logger.debug(f"No processed_ms_format results to {in_name} found")
        return None

    groups = results.groupdict()

    logger.debug(f"Matched groups are: {groups}")

    channel_range = (int(groups["chl"]), int(groups["chh"])) if groups["chl"] else None

    return ProcessedNameComponents(
        sbid=groups["sbid"],
        field=groups["field"],
        beam=groups["beam"],
        spw=groups["spw"],
        round=groups["round"],
        pol=groups["pol"],
        channel_range=channel_range,
    )


def create_path_from_processed_name_components(
    processed_name_components: ProcessedNameComponents, parent_path: Path | None = None
) -> Path:
    """Given an input ProcessedNameComponents create the corresponding path

    Args:
        processed_name_components (ProcessedNameComponents): The naming specification to create
        parent_path (Path | None, optional): The parent directory of the output path. Defaults to None.

    Returns:
        Path: A directory with following the specification of the input ProcessedNameComponents
    """

    components = []

    # Operate over each field in order and create the formatted field appropriately
    if processed_name_components.sbid:
        components.append(f"SB{processed_name_components.sbid}")
    if processed_name_components.field:
        components.append(processed_name_components.field)
    if processed_name_components.beam:
        components.append(f"beam{int(processed_name_components.beam):02d}")
    if processed_name_components.spw:
        components.append(f"spw{int(processed_name_components.spw):02d}")
    if processed_name_components.round:
        components.append(f"round{int(processed_name_components.round)}")
    if processed_name_components.pol:
        components.append(f"{processed_name_components.pol}")
    if processed_name_components.channel_range:
        components.append(
            f"ch{processed_name_components.channel_range[0]:04d}-{processed_name_components.channel_range[1]:04d}"
        )

    # Join then add the parent path
    name = ".".join(components)
    out_path = Path(name)

    if parent_path:
        out_path = Path(parent_path) / out_path

    return out_path


def extract_components_from_name(
    name: str | Path,
) -> RawNameComponents | ProcessedNameComponents | CASDANameComponents:
    """Attempts to break down a file name of a recognised format into its principal compobnents.
    Presumably this is a measurement set or something derived from it (i.e. images).

    There are two formats currently recognised:
    - the raw measurement set format produced by the ASKAP ingest system (date, time, beam, spw, underscore delimited)
    - a formatted name produced by flint (SBID, field, beam, spw, dot delimited)

    Internally this function attempts to run two regular expression filters against the input,
    and returns to the set of components that a filter has matched.

    Args:
        name (Union[str,Path]): The name to examine to search for the beam number.

    Raises:
        ValueError: Raised if the name was not was not successfully matched against the known format

    Returns:
        Union[RawNameComponents,ProcessedNameComponents,CASDANamedComponents]: The extracted name components within a name
    """
    name = str(Path(name).name)
    results_raw = raw_ms_format(in_name=name)
    results_formatted = processed_ms_format(in_name=name)
    results_casda = casda_ms_format(in_name=name)

    if all([res is None for res in (results_raw, results_formatted, results_casda)]):
        raise ValueError(f"Unrecognised file name format for {name=}. ")

    matched = [
        res
        for res in (results_raw, results_formatted, results_casda)
        if res is not None
    ]

    if len(matched) > 1:
        logger.info(
            f"The {name=} was recognised as more than one format. Selecting the simplest.  "
        )
        logger.info(f"{results_raw=} {results_formatted=} ")

    results = matched[0]

    return results


def split_images(
    images: list[Path],
    by: str = "pol",
) -> dict[str, list[Path]]:
    """Split a list of images by a given field. This is intended to be used
    when a set of images are to be split by a common field, such as polarisation.

    Args:
        images (List[Path]): The images to split
        by (str, optional): The field to split the images by. Defaults to "pol".

    Returns:
        Dict[str,List[Path]]: A dictionary of the images split by the field
    """
    logger.info(f"Splitting {images=} by {by=}")
    split_dict: dict[str, list[Path]] = {}
    for image in images:
        components = extract_components_from_name(name=image)

        try:
            field = getattr(components, by)
            if field is None:
                raise AttributeError(f"{field=} is None")

        except AttributeError as e:
            msg = f"Failed to extract {by=} from {image=}"
            raise NamingException(msg) from e

        if field not in split_dict:
            split_dict[field] = []
        split_dict[field].append(image)

    return split_dict


def split_and_get_images(
    images: list[Path],
    get: str,
    by: str = "pol",
) -> list[Path]:
    """Split a list of images by a given field and return the images that match
    the field of interest.

    Args:
        images (list[Path]): The images to split
        get (str): The field to extract from the split images
        by (str, optional): How to split the images. Defaults to "pol".

    Raises:
        ValueError: If the field to extract is not found in the split images

    Returns:
        list[Path]: The images that match the field of interest
    """
    split_dict = split_images(images=images, by=by)

    split_list = split_dict.get(get, None)
    if split_list is None:
        raise ValueError(f"Failed to get {get=} from {split_dict=}")

    return split_list


def extract_beam_from_name(name: str | Path) -> int:
    """Attempts to extract the beam number from some input name should it follow a
    known naming convention.

    Args:
        name (Union[str,Path]): The name to examine to search for the beam number.

    Raises:
        ValueError: Raised if the name was not was not successfully matched against the known format

    Returns:
        int: Beam number that extracted from the input name
    """

    results = extract_components_from_name(name=name)
    if results is None or results.beam is None:
        raise ValueError(
            f"Failed to convert to processed name format and find beam: {name=} {results=}"
        )

    return int(results.beam)


def create_ms_name(
    ms_path: Path, sbid: int | None = None, field: str | None = None
) -> str:
    """Create a consistent naming scheme for measurement sets. At present
    it is intended to be used for splitting fields from raw measurement
    sets, but can be expanded.

    Args:
        ms_path (Path): The measurement set being considered. A RawNameComponents will be constructed against it.
        sbid (Optional[int], optional): An explicit SBID to include in the name, otherwise one will attempted to be extracted the the ms path. If these fail the sbid is set of 00000. Defaults to None.
        field (Optional[str], optional): The field that this measurement set will contain. Defaults to None.

    Returns:
        str: The filename of the measurement set
    """

    ms_path = Path(ms_path).absolute()
    ms_name_list: list[Any] = []

    format_components = extract_components_from_name(name=ms_path)

    # Use the explicit SBID is provided, otherwise attempt
    # to extract it
    sbid_text = "SB0000"
    if sbid:
        sbid_text = f"SB{sbid}"
    elif (
        not isinstance(format_components, RawNameComponents) and format_components.sbid
    ):
        sbid_text = f"SB{format_components.sbid}"
    else:
        try:
            sbid = get_sbid_from_path(path=ms_path)
            sbid_text = f"SB{sbid}"
        except Exception as e:
            logger.warning(f"{e}, using default {sbid_text}")
    ms_name_list.append(sbid_text)

    field = (
        field
        if field
        else (
            format_components.field
            if not isinstance(format_components, RawNameComponents)
            and format_components.field
            else None
        )
    )
    if field:
        ms_name_list.append(field)

    if format_components:
        if format_components.beam is not None:
            ms_name_list.append(f"beam{int(format_components.beam):02d}")
        if format_components.spw:
            ms_name_list.append(f"spw{format_components.spw}")

    ms_name_list.append("ms")
    ms_name = ".".join(ms_name_list)

    return ms_name


class AegeanNames(NamedTuple):
    """Base names that would be used in various Aegean related tasks"""

    bkg_image: Path
    """Background map computed by BANE"""
    rms_image: Path
    """RMS noise map computed by BANE"""
    comp_cat: Path
    """Component catalogue produced by the aegean source finder"""
    ds9_region: Path
    """DS9 region overlay file"""
    resid_image: Path
    """Residual map after subtracting component catalogue produced by AeRes"""


def create_aegean_names(base_output: str) -> AegeanNames:
    """Create the expected names for aegean and its tools.

    Args:
        base_output (str): The base name that aegean outputs are built from.

    Returns:
        AegeanNames: A collection of names to be produced by Aegean related tasks
    """
    base_output = str(base_output)

    return AegeanNames(
        bkg_image=Path(f"{base_output}_bkg.fits"),
        rms_image=Path(f"{base_output}_rms.fits"),
        comp_cat=Path(f"{base_output}_comp.fits"),
        ds9_region=Path(f"{base_output}_overlay.reg"),
        resid_image=Path(f"{base_output}_residual.fits"),
    )


class LinmosNames(NamedTuple):
    """Creates expected output names for the yandasoft linmos task."""

    image_fits: Path
    """Path to the final fits co-added image"""
    weight_fits: Path
    """Path to the weights fits image created when co-adding images"""
    parset_output_path: Path
    """Path to the output parset generated"""


def create_linmos_names(
    name_prefix: str | Path, parset_output_path: Path | None = None
) -> LinmosNames:
    """This creates the names that would be output but the yandasoft
    linmos task. It returns the names for the linmos and weight maps
    that linmos would create. These names will have the .fits extension
    with them, but be aware that when the linmos parset if created
    these are omitted.

    Args:
        name_prefix (str | Path): The prefix of the filename that will be used to create the linmos and weight file names.

    Returns:
        LinmosNames: Collection of expected filenames
    """
    name_prefix = str(name_prefix) if isinstance(name_prefix, Path) else name_prefix

    logger.info(f"Linmos name prefix is: {name_prefix}")
    return LinmosNames(
        image_fits=Path(f"{name_prefix}.linmos.fits"),
        weight_fits=Path(f"{name_prefix}.weight.fits"),
        parset_output_path=Path(f"{name_prefix}_parset.txt")
        if parset_output_path is None
        else parset_output_path,
    )


def create_linmos_base_path(
    input_images: list[Path],
    additional_suffixes: str | None = None,
) -> Path:
    """Create the base path of a ``yandasoft linmos`` given a set of input images.
    The default operation is to form the name from the common processed name fields
    amount all of the input images.


    Args:
        input_images (list[Path] | None, optional): If provided the common fields of the input images are used as basis of the path. Defaults to None.
        additional_suffixes (str | None, optional): Any additional suffixes to append. Defaults to None.


    Returns:
        Path: The full path of the parset file
    """

    # Unless something has been specified, we make it up
    logger.info(f"Combining images {input_images}")
    output_name = create_name_from_common_fields(
        in_paths=tuple(input_images), additional_suffixes=additional_suffixes
    )
    out_dir = output_name.parent
    logger.info(f"Base output image name will be: {output_name}")
    assert out_dir is not None, f"{out_dir=}, which should not happen"

    return output_name.absolute()


def get_sbid_from_path(path: Path) -> int:
    """Attempt to extract the SBID of a observation from a path. It is a fairly simple ruleset
    that follows the typical use cases that are actually in practise. There is no mechanism to
    get the SBID from the measurement set meta-data.

    If the path provided ends in a .ms suffix, the parent directory is assumed to be named
    the sbid. Otherwise, the name of the subject directory is. A test is made to ensure the
    sbid is made up of integers only.

    Args:
        path (Path): The path that contains the sbid to extract.

    Raises:
        ValueError: Raised when the SBID extracted from the path is not all digits

    Returns:
        int: The sbid extracted
    """
    path = Path(path)
    path_suffix = path.suffix

    logger.debug(f"Suffix of {path} is {path_suffix}")

    if path_suffix.endswith(".ms"):
        logger.debug("This is a measurement set, so sbid must be the parent directory")
        sbid = path.parent.name
    else:
        sbid = path.name

    if not sbid.isdigit():
        raise ValueError(
            f"Extracted {sbid=} from {path!s} failed appears to be non-conforming - it is not a number! "
        )

    return int(sbid)


def get_potato_output_base_path(ms_path: Path) -> Path:
    """Return the base name for potato peel.

    Args:
        ms_path (Path): Input measurement set that follows the FLINT style process name format

    Returns:
        Path: Output base name to use
    """

    ms_components = processed_ms_format(in_name=ms_path)
    assert ms_components is not None, f"{ms_components=}, which should not be possible"
    output_components = (
        f"SB{ms_components.sbid}.{ms_components.field}.beam{ms_components.beam}.potato"
    )

    output_path = ms_path.parent / output_components
    logger.info(f"Output potato base name: {output_path}")

    return output_path


def get_aocalibrate_output_path(
    ms_path: Path, include_preflagger: bool, include_smoother: bool
) -> Path:
    """Create a name for an aocalibrate style bandpass solution.

    Args:
        ms_path (Path): Path of the measurement set that the aocalibrate file will be created for
        include_preflagger (bool): Whether to include the ``.preflagged`` term to indicate that the preflagger has been executed
        include_smoother (bool): Whether to include the ``.smoothed`` term to included that bandpas smoothing has been performed

    Returns:
        Path: The constructed output path of the solutions file. This include the parent directory of the input measurement set
    """
    ms_components = processed_ms_format(in_name=ms_path)
    assert ms_components is not None, f"{ms_components=}, which should not be possible"
    output_components = [
        f"SB{ms_components.sbid}.{ms_components.field}.beam{ms_components.beam}"
    ]

    if ms_components.spw:
        output_components.append(f"spw{ms_components.spw}")

    output_components.append("aocalibrate")

    if include_preflagger:
        output_components.append("preflagged")
    if include_smoother:
        output_components.append("smoothed")

    output_components.append("bin")

    output_name = ".".join(output_components)
    output_path = ms_path.parent / output_name
    logger.info(f"Constructed {output_path}")

    return output_path


class FITSMaskNames(NamedTuple):
    """Contains the names of the FITS images created when creating a mask image/
    These are only the names, and do not mean that they are necessarily created.
    """

    mask_fits: Path
    """Name of the mask FITS file"""
    signal_fits: Path | None = None
    """Name of the signal FITS file"""


def create_fits_mask_names(
    fits_image: str | Path, include_signal_path: bool = False
) -> FITSMaskNames:
    """Create the names that will be used when generate FITS mask products

    Args:
        fits_image (Union[str,]Path): Base name of the output files
        include_signal_path (bool, optional): If True, also include ``signal_fits`` in the output. Defaults to False.

    Returns:
        FITSMaskNames: collection of names used for the signal and mask FITS images
    """
    fits_image = Path(fits_image)

    fits_signal = (
        fits_image.with_suffix(".signal.fits") if include_signal_path else None
    )
    fits_mask = fits_image.with_suffix(".mask.fits")

    return FITSMaskNames(signal_fits=fits_signal, mask_fits=fits_mask)
