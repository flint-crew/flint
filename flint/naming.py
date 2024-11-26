"""Attempts to centralise components to do with naming of pipeline files and data
products.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

from flint.logging import logger
from flint.options import MS


def get_fits_cube_from_paths(paths: List[Path]) -> List[Path]:
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


def create_image_cube_name(
    image_prefix: Path, mode: str, suffix: str = "cube.fits"
) -> Path:
    """Create a consistent naming scheme when combining images into cube images. Intended to
    be used when combining many subband images together into a single cube.

    The name returned will be:
    >> {image_prefix}.{mode}.{suffix}

    Args:
        image_prefix (Path): The unique path of the name. Generally this is the common part among the input planes
        mode (str): Additional mode to add to the file name
        suffix (str, optional): The final suffix to appended. Defaults to ".cube.fits".

    Returns:
        Path: The final path and file name
    """
    # NOTE: This is likely a function to grow in time as more imaging and pipeline modes added. Putting
    # it here for future proofing
    output_cube_name = f"{str(Path(image_prefix))}.{mode}.{suffix}"

    return Path(output_cube_name)


def create_imaging_name_prefix(
    ms: Union[MS, Path],
    pol: Optional[str] = None,
    channel_range: Optional[Tuple[int, int]] = None,
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

    ms_path = MS.cast(ms=ms).path

    name = ms_path.stem
    if pol:
        name = f"{name}.{pol.lower()}"
    if channel_range:
        name = f"{name}.ch{channel_range[0]}-{channel_range[1]}"

    return name


def get_beam_resolution_str(mode: str, marker: Optional[str] = None) -> str:
    """Map a beam resolution mode to an appropriate suffix. This
    is located her in anticipation of other imaging modes.

    Supported modes are: 'optimal', 'fixed', 'raw'

    Args:
        mode (str): The mode of image resolution to use.
        marker (Optional[str], optional): Append the marker to the end of the returned mode string. If None mode string is returned. Defaults to None.

    Raises:
        ValueError: Raised when an unrecognised mode is supplied

    Returns:
        str: The appropriate string for mapped mode
    """
    # NOTE: Arguably this is a trash and needless function. Adding it
    # in case other modes are ever needed or referenced. No idea whether
    # it will ever been needed and could be removed in future.
    supported_modes: Dict[str, str] = dict(optimal="optimal", fixed="fixed", raw="raw")
    if mode.lower() not in supported_modes.keys():
        raise ValueError(
            f"Received {mode=}, supported modes are {supported_modes.keys()}"
        )

    mode_str = supported_modes[mode.lower()]

    return mode_str + marker if marker else mode_str


def get_selfcal_ms_name(in_ms_path: Path, round: int = 1) -> Path:
    """Create the new output MS path that will be used for self-calibration. The
    output measurement set path will include a roundN.ms suffix, where N is the
    round. If such a suffic already exists from an earlier self-calibration round,
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
        name = f"{name_str[:span[0]]}.round{round}.ms"
    else:
        name = f"{str(in_ms_path.stem)}.round{round}.ms"
    out_ms_path = in_ms_path.parent / name

    assert (
        in_ms_path != out_ms_path
    ), f"{in_ms_path=} and {out_ms_path=} match. Something went wrong when creating new self-cal name. "

    return out_ms_path


def add_timestamp_to_path(
    input_path: Union[Path, str], timestamp: Optional[datetime] = None
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
    spw: Optional[str] = None
    """If multiple MS were written as the data were in a high-frequency resolution mode, which segment"""
    alias: Optional[str] = None
    """Older ASKAP MSs could be packed with multiple fields. The ASKAP pipeline holds this field as an alias. They are now the same in almost all cases as the field. """
    format: str = "science"
    """What the format / type of the data the MS is. """


def casda_ms_format(in_name: Union[str, Path]) -> Union[CASDANameComponents, None]:
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
    spw: Optional[str] = None
    """If multiple MS were written as the data were in a high-frequency resolution mode, which segment"""


def raw_ms_format(in_name: str) -> Union[None, RawNameComponents]:
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
    beam: str
    """The beam of the observation processed"""
    spw: Optional[str] = None
    """The SPW of the observation. If there is only one spw this is None."""
    round: Optional[str] = None
    """The self-calibration round detected. This might be represented as 'noselfcal' in some image products, e.g. linmos. """
    pol: Optional[str] = None
    """The polarisation component, if it exists, in a filename. Examples are 'i','q','u','v'. Could be combinations in some cases depending on how it was created (e.g. based on wsclean pol option). """


def processed_ms_format(
    in_name: Union[str, Path],
) -> Union[ProcessedNameComponents, None]:
    """Will take a formatted name (i.e. one derived from the flint.naming.create_ms_name)
    and attempt to extract its main components. This includes the SBID, field, beam and spw.

    Args:
        in_name (Union[str, Path]): The name that needs to be broken down into components

    Returns:
        Union[FormatedNameComponents,None': A structure container the sbid, field, beam and spw. None is returned if can not be parsed.
    """

    in_name = in_name.name if isinstance(in_name, Path) else in_name

    logger.debug(f"Matching {in_name}")
    # A raw string is used to avoid bad unicode escaping
    regex = re.compile(
        r"^SB(?P<sbid>[0-9]+)\.(?P<field>.+)\.beam(?P<beam>[0-9]+)((\.spw(?P<spw>[0-9]+))?)((\.round(?P<round>[0-9]+))?)((\.(?P<pol>(i|q|u|v|xx|yy|xy|yx)+))?)*"
    )
    results = regex.match(in_name)

    if results is None:
        logger.debug(f"No processed_ms_format results to {in_name} found")
        return None

    groups = results.groupdict()

    logger.debug(f"Matched groups are: {groups}")

    return ProcessedNameComponents(
        sbid=groups["sbid"],
        field=groups["field"],
        beam=groups["beam"],
        spw=groups["spw"],
        round=groups["round"],
        pol=groups["pol"],
    )


def extract_components_from_name(
    name: Union[str, Path],
) -> Union[RawNameComponents, ProcessedNameComponents, CASDANameComponents]:
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


def extract_beam_from_name(name: Union[str, Path]) -> int:
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

    return int(results.beam)


def create_ms_name(
    ms_path: Path, sbid: Optional[int] = None, field: Optional[str] = None
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
    ms_name_list: List[Any] = []

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


def create_linmos_names(name_prefix: str) -> LinmosNames:
    """This creates the names that would be output but the yandasoft
    linmos task. It returns the names for the linmos and weight maps
    that linmos would create. These names will have the .fits extension
    with them, but be aware that when the linmos parset if created
    these are omitted.

    Args:
        name_prefix (str): The prefix of the filename that will be used to create the linmos and weight file names.

    Returns:
        LinmosNames: Collection of expected filenames
    """
    logger.info(f"Linmos name prefix is: {name_prefix}")
    return LinmosNames(
        image_fits=Path(f"{name_prefix}.linmos.fits"),
        weight_fits=Path(f"{name_prefix}.weight.fits"),
    )


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
            f"Extracted {sbid=} from {str(path)} failed appears to be non-conforming - it is not a number! "
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
    signal_fits: Optional[Path] = None
    """Name of the signal FITS file"""


def create_fits_mask_names(
    fits_image: Union[str, Path], include_signal_path: bool = False
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
