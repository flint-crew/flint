"""Summary containers intended to hold general information obtained throughout a processing run."""

from __future__ import (  # Used for mypy/pylance to like the return type of MS.with_options
    annotations,
)

from pathlib import Path
from typing import NamedTuple

import astropy.units as u
from astropy.coordinates import (
    AltAz,
    EarthLocation,
    Latitude,
    Longitude,
    SkyCoord,
    concatenate,
)
from astropy.table import Table
from astropy.time import Time

# Addressing some time interval IERS issue with astropy.
from astropy.utils.iers import conf

from flint.coadd.linmos import LinmosResult
from flint.imager.wsclean import ImageSet, WSCleanResult
from flint.logging import logger
from flint.ms import (
    MS,
    MSSummary,
    describe_ms,
    get_pol_axis_from_ms,
    get_telescope_location_from_ms,
    get_times_from_ms,
)
from flint.naming import get_sbid_from_path, processed_ms_format
from flint.source_finding.aegean import AegeanOutputs
from flint.utils import estimate_skycoord_centre

conf.auto_max_age = None


class FieldSummary(NamedTuple):
    """The main information about a processed field. This structure is
    intended to store critical components that might be accumulated throughout
    processing of a pipeline, and may be most useful when data-products
    are zipped (or removed) throughout. Its intended usage is to hold key
    components that might be used in stages like validation plotting. It is not
    intended to become a catch-all to replace passing through items into
    functions directly.

    There are known issues around serialising astropy units within a dask/prefect environment,
    for example: https://github.com/astropy/astropy/issues/11317,
    This could become important if an instance of this object is exchanged
    between many prefect or dask like delayed tasks. Ye be warned.
    """

    sbid: str
    """SBID of processed field"""
    cal_sbid: str
    """SBID of the bandpass calibrator"""
    field_name: str
    """The name of the field"""
    ms_summaries: tuple[MSSummary, ...] | None = None
    """Summaries of measurement sets used in the processing of the filed"""
    centre: SkyCoord | None = None
    """Centre of the field, which is calculated as the mean position of all phase directions of the `mss` measurement sets"""
    integration_time: int | None = None
    """The integration time of the observation (seconds)"""
    no_components: int | None = None
    """Number of components found from the source finder"""
    holography_path: Path | None = None
    """Path to the file used for holography"""
    round: int | None = None
    """The self-cal round"""
    location: EarthLocation | None = None
    """The location of the telescope stored as (X,Y,Z) in meters"""
    ms_times: Time | None = None
    """The unique scan times of integrations stored in the measurement set"""
    hour_angles: Longitude | None = None
    """Computed hour-angles of the field"""
    elevations: Latitude | None = None
    """Computed elevations of the field"""
    median_rms: float | None = None
    """The meanian RMS computed from an RMS image"""
    beam_summaries: list[BeamSummary] | None = None
    """Summary information from each beam. Contains MSSummary, ImageSet and other information."""
    linmos_image: Path | None = None
    """The path to the linmos image of all beams"""
    pol_axis: float | None = None
    """The orientation of the ASKAP third-axis in radians. """

    def with_options(self, **kwargs) -> FieldSummary:
        prop = self._asdict()
        prop.update(**kwargs)

        return FieldSummary(**prop)


def _get_pol_axis_as_rad(ms: MS | Path) -> float:
    """Helper to get the appropriate pol_axis out of a MS. Prioritises the instrumental third-axis imprinted from fixms"""
    ms = MS.cast(ms=ms)

    # The INSTRUMENT_RECEPTOR_ANGLE comes from fixms and is
    # inserted to preserve the original orientation.

    try:
        pol_axis = get_pol_axis_from_ms(ms=ms, col="INSTRUMENT_RECEPTOR_ANGLE")

        logger.info(f"INSTRUMENT_RECEPTOR_ANGLE obtained pol_axis={pol_axis}")
    except ValueError:
        pol_axis = get_pol_axis_from_ms(ms=ms, col="RECEPTOR_ANGLE")

        # The prefect logger (or maybe the logger in general) does not render the quantity
        logger.info(f"RECEPTOR_ANGLE obtained pol_axis={pol_axis}")

    return pol_axis.to(u.rad).value


# TODO: Need to establise a MSLike type
def add_ms_summaries(field_summary: FieldSummary, mss: list[MS]) -> FieldSummary:
    """Obtain a MSSummary instance to add to a FieldSummary

    Quantities derived from the field centre (hour angles, elevations) are
    also calculated. The field centre position is estimated by taking the
    mean position of all phase directions.

    See `flint.utils.estimate_skycoord_centre`

    Args:
        field_summary (FieldSummary): Existing field summary object to update
        mss (List[MS]): Set of measurement sets to describe

    Returns:
        Tuple[MSSummary]: Results from the inspected set of measurement sets
    """
    logger.info("Adding MS summaries")

    ms_summaries = tuple(map(describe_ms, mss))
    centres_list = [ms_summary.phase_dir for ms_summary in ms_summaries]
    if len(centres_list) == 0:
        raise ValueError("No phase directions found in the MSs")
    elif len(centres_list) == 1:
        centre = centres_list[0]
    else:
        centres = concatenate(centres_list)
        centre = estimate_skycoord_centre(sky_positions=centres)

    telescope = field_summary.location
    ms_times = field_summary.ms_times
    centre_altaz = centre.transform_to(AltAz(obstime=ms_times, location=telescope))
    hour_angles = centre_altaz.az.to(u.hourangle)  # type: ignore
    elevations = centre_altaz.alt.to(u.deg)  # type: ignore

    # The INSTRUMENT_RECEPTOR_ANGLE comes from fixms and is
    # inserted to preserve the original orientation.
    pol_axis = _get_pol_axis_as_rad(ms=mss[0])

    field_summary = field_summary.with_options(
        ms_summaries=ms_summaries,
        centre=centre,
        hour_angles=hour_angles,
        elevations=elevations,
        pol_axis=pol_axis,
    )

    return field_summary


def add_rms_information(
    field_summary: FieldSummary, aegean_outputs: AegeanOutputs
) -> FieldSummary:
    """Add information derived from an RMS image and component catalogue
    to an existing `FieldSummary` instance. Some properteries, such as
    the center position, number of components etc, are taken directly
    from source finder products.

    On the center position -- there is not (at the moment) a simple
    way of getting the center position of a field. So the image
    itself is used to grab it.

    Other properties that require components of a measurement set,
    including the time and position, are also derived using existing
    fields which are created when a new instance is made.

    Args:
        field_summary (FieldSummary): Existing field summary object to update
        aegean_outputs (AegeanOutputs): Products of a source finding run

    Returns:
        FieldSummary: Updated field summary object
    """
    no_components = len(Table.read(aegean_outputs.comp))

    field_summary = field_summary.with_options(
        no_components=no_components,
    )

    return field_summary


def add_linmos_fits_image(
    field_summary: FieldSummary, linmos_command: LinmosResult
) -> FieldSummary:
    """Extract the path of the linmos fits image from the LinmosResult
    the co-added the field

    Args:
        field_summary (FieldSummary): Existing field summary to update
        linmos_command (LinmosResult): Instance of a completed linmos command that coadded a field

    Returns:
        FieldSummary: The updated field summary object with the linmos fits image added
    """
    assert isinstance(linmos_command, LinmosResult), (
        f"{linmos_command=} is type {type(linmos_command)}, expected LinmosResult"
    )

    image_fits = linmos_command.image_fits
    field_summary = field_summary.with_options(linmos_image=image_fits)

    return field_summary


def update_field_summary(
    field_summary: FieldSummary,
    aegean_outputs: AegeanOutputs | None = None,
    mss: list[MS] | None = None,
    linmos_command: LinmosResult | None = None,
    **kwargs,
) -> FieldSummary:
    """Update an existing `FieldSummary` instance with additional information.

    If special steps are required to be carried out based on a known input they will be.
    Otherwise all additional keyword arguments are passed through to the `FieldSummary.with_options`.

    Args:
        field_summary (FieldSummary): Field summary object to update
        aegean_outputs (Optional[AegeanOutputs], optional): Will add RMS and aegean related properties. Defaults to None.
        mss (Optional[Collection[MS]], optionals): Set of measurement sets to describe
        linmos_command (Optional[LinmosResult], optional): The linmos command created when co-adding all beam images together

    Returns:
        FieldSummary: An updated field summary objects
    """

    if aegean_outputs:
        field_summary = add_rms_information(
            field_summary=field_summary, aegean_outputs=aegean_outputs
        )

    if mss:
        field_summary = add_ms_summaries(field_summary=field_summary, mss=mss)

    if linmos_command:
        field_summary = add_linmos_fits_image(
            field_summary=field_summary, linmos_command=linmos_command
        )

    field_summary = field_summary.with_options(**kwargs)

    logger.info(f"Updated {field_summary=}")
    return field_summary


def create_field_summary(
    mss: list[MS | Path],
    cal_sbid_path: Path | None = None,
    holography_path: Path | None = None,
    aegean_outputs: AegeanOutputs | None = None,
    **kwargs,
) -> FieldSummary:
    """Create a field summary object using a measurement set.

    All other keyword arguments are passed directly through to `FieldSummary`

    Args:
        ms (Union[MS, Path]): Measurement set information will be pulled from
        cal_sbid_path (Optional[Path], optional): Path to an example of a bandpass measurement set. Defaults to None.
        holography_path (Optional[Path], optional): The holography fits cube used (or will be) to linmos. Defaults to None.
        aegean_outputs (Optional[AegeanOutputs], optional): Should RMS / source information be added to the instance. Defaults to None.
        mss (Optional[Collection[MS]], optionals): Set of measurement sets to describe

    Returns:
        FieldSummary: A summary of a field
    """

    logger.info("Creating field summary object")

    mss = [MS.cast(ms=ms) for ms in mss]

    # TODO: A check here to ensure all MSs are in a consistent format
    # and are from the same field
    ms = MS.cast(ms=mss[0])

    ms_components = processed_ms_format(in_name=ms.path)
    assert ms_components is not None, (
        f"MS ({ms.path}) is not in a flint-processed format!"
    )
    sbid = str(ms_components.sbid)
    field = ms_components.field

    assert field is not None, f"Field name is empty in {ms_components=} from {ms.path=}"

    try:
        cal_sbid = (
            str(get_sbid_from_path(path=cal_sbid_path)) if cal_sbid_path else None
        )
    except ValueError:
        cal_sbid = "-9999"
        logger.info(f"Extracting SBID from {cal_sbid_path=} failed. Using {cal_sbid=}")

    ms_times = get_times_from_ms(ms=ms)
    integration = ms_times.ptp().to(u.second).value
    location = get_telescope_location_from_ms(ms=ms)

    pol_axis = _get_pol_axis_as_rad(ms=ms)

    field_summary = FieldSummary(
        sbid=sbid,
        field_name=field,
        cal_sbid=f"{cal_sbid}",  # could be None, and that's OK
        location=location,
        integration_time=integration,
        holography_path=holography_path,
        ms_times=Time([ms_times.min(), ms_times.max()]),
        pol_axis=pol_axis,
        **kwargs,
    )

    field_summary = add_ms_summaries(
        field_summary=field_summary, mss=[MS.cast(ms=ms) for ms in mss]
    )

    if aegean_outputs:
        field_summary = add_rms_information(
            field_summary=field_summary, aegean_outputs=aegean_outputs
        )

    return field_summary


class BeamSummary(NamedTuple):
    """Structure intended to collect components from a measurement set
    as it is being processed through a continuum imaging pipeline
    """

    ms_summary: MSSummary
    """A summary object of a measurement set"""
    image_set: ImageSet | None = None
    """A set of images that have been created from the measurement set represented by `summary`"""
    components: AegeanOutputs | None = None
    """The source finding components from the aegean source finder"""

    def with_options(self, **kwargs) -> BeamSummary:
        prop = self._asdict()
        prop.update(**kwargs)

        return BeamSummary(**prop)


def create_beam_summary(
    ms: MS | Path,
    image_set: ImageSet | WSCleanResult | None = None,
    components: AegeanOutputs | None = None,
) -> BeamSummary:
    """Create a summary of a beam

    Args:
        ms (Union[MS, Path]): The measurement set being considered
        image_set (Optional[ImageSet], optional): Images produced from an imager. Defaults to None.
        components (Optional[AegeanOutputs], optional): Source finding output components. Defaults to None.

    Returns:
        BeamSummary: Summary object of a beam
    """
    ms = MS.cast(ms=ms)
    logger.info(f"Creating BeamSummary for {ms.path=}")
    ms_summary = describe_ms(ms=ms)

    # TODO: Another example where a .cast type method could be useful
    # or where a standardised set of attributes with a HasImageSet type
    if image_set:
        image_set = (
            image_set if isinstance(image_set, ImageSet) else image_set.image_set
        )

    beam_summary = BeamSummary(
        ms_summary=ms_summary, image_set=image_set, components=components
    )

    return beam_summary
