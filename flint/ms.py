"""Operations related to measurement sets

Measurement sets are represented by the `MS` class. It describes
the path to a measurement set and any activate columns that should
be used for certain operations. Throughout `flint` steps are
generally carried out against the named column described by the
`column` attribute.
"""

from __future__ import annotations

import shutil
from argparse import ArgumentParser
from contextlib import contextmanager
from curses.ascii import controlnames
from os import PathLike
from pathlib import Path
from typing import NamedTuple

import astropy.units as u
import numpy as np
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
from casacore.tables import table, taql
from fixms.fix_ms_corrs import fix_ms_corrs
from fixms.fix_ms_dir import fix_ms_dir

from flint.casa import copy_with_mstranform
from flint.logging import logger
from flint.naming import create_ms_name
from flint.options import MS
from flint.utils import copy_directory, rsync_copy_directory


class MSSummary(NamedTuple):
    """Small structure to contain overview of a MS"""

    unflagged: int
    """Number of unflagged records"""
    flagged: int
    """Number of flagged records"""
    flag_spectrum: np.ndarray
    """Flagged spectral channels"""
    fields: list[str]
    """Collection of unique field names from the FIELDS table"""
    ants: list[int]
    """Collection of unique antennas"""
    beam: int
    """The ASKAP beam number of the measurement set"""
    path: Path
    """Path to the measurement set that is being represented"""
    phase_dir: SkyCoord
    """The phase direction of the measurement set, which will be where the image will be centred"""
    spw: int | None = None
    """Intended to be used with ASKAP high-frequency resolution modes, where the MS is divided into SPWs"""


# TODO: Some common MS validation functions?
# - list / number of fields
# - new name function (using names / beams)
# - check to see if fix_ms_dir / fix_ms_corrs
# - delete column/rename column


@contextmanager
def critical_ms_interaction(
    input_ms: Path, copy: bool = False, suffix: str = ".critical"
):
    """A context manager that can be used to register a measurement set as
    entering a critical segment, i.e. phase rotation. If this stage were to
    fail, then future operations against said measurement set might be
    nonsense. This mechanism is intended to make it clear that the measurement
    set is in a dangerous part of code.

    Failure to return the MS to its original name (or rename the copy) highlights
    this failed stage.

    Args:
        input_ms (Path): The measurement set to monitor.
        copy (bool, optional): If True, a copy of the MS is made with the suffix supplied. Otherwise, the MS is siomply renamed. Defaults to False.
        suffix (str, optional): Suffix indicating the MS is in the dangerous stage. Defaults to '.critical'.

    Yields:
        Path: Resource location of the measurement set being processed
    """
    input_ms = Path(input_ms)
    output_ms: Path = Path(input_ms.with_suffix(suffix))

    # Make sure that the measurement set to preserve exists, and there is not already
    # a target output measurement set already on disk. This second check would be
    # useful in copy==True mode, ya seadog
    assert input_ms.exists(), f"The input measurement set {input_ms} does not exist. "
    assert not output_ms.exists(), (
        f"The output measurement set {output_ms} already exists. "
    )
    logger.info(f"Critical section for {input_ms=}")
    if copy:
        rsync_copy_directory(target_path=input_ms, out_path=output_ms)
    else:
        input_ms.rename(target=output_ms)

    try:
        yield output_ms
    except Exception as e:
        logger.error(
            f"An error occurred when interacting with {input_ms} during a critical stage. "
        )
        raise e

    # If we get to here, things worked successfully, and we
    # should return things back to normal.
    if copy:
        shutil.rmtree(input_ms)
        output_ms.rename(input_ms)
    else:
        output_ms.rename(target=input_ms)


def get_field_id_for_field(ms: MS | Path, field_name: str) -> int | None:
    """Return the FIELD_ID for an elected field in a measurement set

    Args:
        ms (Union[MS, Path]): The measurement set to inspect
        field_name (str): The desired name of the field to find the FIELD_ID for

    Raises:
        ValueError: Raised when more than one unique FIELD_ID is found for a single field

    Returns:
        Union[int, None]: The FIELD_ID as an `int` if the field is found. `None` if it is not found.
    """
    ms_path = ms if isinstance(ms, Path) else ms.path

    with table(f"{ms_path!s}/FIELD", readonly=True, ack=False) as tab:
        # The ID is _position_ of the matching row in the table.
        field_names = tab.getcol("NAME")
        field_idx = np.argwhere([fn == field_name for fn in field_names])[0]

        if len(field_idx) == 0:
            return None
        elif len(field_idx) > 1:
            raise ValueError(
                f"More than one matching field name found. This should not happen. {field_name=} {field_names=}"
            )

        field_idx = field_idx[0]
        logger.info(f"{field_name} FIELD_ID is {field_idx}")

    return field_idx


def get_beam_from_ms(ms: MS | Path) -> int:
    """Lookup the ASKAP beam number from a measurement set.

    Args:
        ms (Union[MS, Path]): The measurement set to inspect. If `MS`, the attached path is used.

    Returns:
        int: The beam ID number
    """
    ms_path = ms if isinstance(ms, Path) else ms.path

    with table(str(ms_path), readonly=True, ack=False) as tab:
        uniq_beams = sorted(list(set(tab.getcol("FEED1"))))

    assert len(uniq_beams) == 1, (
        f"Expected {ms_path!s} to contain a single beam, found {len(uniq_beams)}: {uniq_beams=}"
    )

    return uniq_beams[0]


def get_freqs_from_ms(ms: MS | Path) -> np.ndarray:
    """Return the frequencies observed from an ASKAP Measurement set.
    Some basic checks are performed to ensure they conform to some
    expectations.

    Args:
        ms (Union[MS, Path]): Measurement set to inspect

    Returns:
        np.ndarray: A squeeze array of frequencies, in Hertz.
    """
    ms = MS.cast(ms)

    with table(f"{ms.path!s}/SPECTRAL_WINDOW", readonly=True, ack=False) as tab:
        freqs = tab.getcol("CHAN_FREQ")

    freqs = np.squeeze(freqs)
    assert len(freqs.shape) == 1, (
        f"Frequency axis has dimensionality greater than one. Not expecting that. {len(freqs.shape)}"
    )

    return freqs


def get_phase_dir_from_ms(ms: MS | Path) -> SkyCoord:
    """Extract the phase direction from a measurement set.

    If more than one phase direction is found an AssertError will
    be raised.

    Args:
        ms (Union[MS, Path]): The measurement set to get the phase direction from

    Returns:
        SkyCoord: The phase direction the measurement set is directed towards
    """
    ms = MS.cast(ms)

    with table(f"{ms.path!s}/FIELD", readonly=True, ack=False) as tab:
        phase_dir = tab.getcol("PHASE_DIR")[0]

    assert phase_dir.shape[0] == 1, "More than one phase direction found. "

    phase_sky = SkyCoord(*phase_dir[0], unit=(u.rad, u.rad))

    return phase_sky


def get_times_from_ms(ms: MS | Path) -> Time:
    """Return the observation times from an ASKAP Measurement set.

    Args:
        ms (Union[MS, Path]): Measurement set to inspect

    Returns:
        Time: The observation times
    """
    # Get the time of the observation
    ms = MS.cast(ms)
    with table(str(ms.path), ack=False) as tab:
        times = Time(tab.getcol("TIME") * u.s, format="mjd")

    return times


def get_telescope_location_from_ms(ms: MS | Path) -> EarthLocation:
    """Return the telescope location from an ASKAP Measurement set.

    Args:
        ms (Union[MS, Path]): Measurement set to inspect

    Returns:
        EarthLocation: The telescope location
    """
    ms = MS.cast(ms)
    # Get the position of the observatory
    with table(str(ms.path / "ANTENNA"), ack=False) as tab:
        pos = EarthLocation.from_geocentric(
            *tab.getcol("POSITION")[0] * u.m  # First antenna is fine
        )
    return pos


def get_pol_axis_from_ms(
    ms: MS | Path, feed_idx: int | None = None, col: str = "RECEPTOR_ANGLE"
) -> u.Quantity:
    """Get the polarization axis from the ASKAP MS. Checks are performed
    to ensure this polarisation axis angle is constant throughout the observation.


    Args:
        ms (Union[MS, Path]): The path to the measurement set that will be inspected
        feed_idx (Optional[int], optional): Specify the entry in the FEED
        table of `ms` to return. This might be required when a subset of a
        measurement set has been extracted from an observation with a varying
        orientation.
        col (str, optional): The column to extract the polarization angle from.

    Returns:
        astropy.units.Quantity: The rotation of the PAF throughout the observing.
    """
    ms = MS.cast(ms=ms)

    # The INSTRUMENT_RECEPTOR_ANGLE is inserted from fixms.
    _known_cols = ("RECEPTOR_ANGLE", "INSTRUMENT_RECEPTOR_ANGLE")
    if col not in _known_cols:
        raise ValueError(f"Unknown column {col=}, please use one of {_known_cols}")

    with table((ms.path / "FEED").as_posix(), readonly=True, ack=False) as tf:
        if col not in tf.colnames():
            raise ValueError(f"{col=} not in the column names available. ")

        ms_feed = tf.getcol(col) * u.rad
        # PAF is at 45deg to feeds
        # 45 - feed_angle = pol_angle
        pol_axes = -(ms_feed - (45.0 * u.deg))  # type: ignore

    if feed_idx is None:
        assert (ms_feed[:, 0] == ms_feed[0, 0]).all() & (
            ms_feed[:, 1] == ms_feed[0, 1]
        ).all(), f"The {col} changes with time, please check the MS"

        feed_idx = 0

    logger.debug(f"Extracting the third-axis orientation for {feed_idx=}")
    pol_ang = pol_axes[feed_idx, 0].to(u.deg)

    assert pol_ang is not None, f"{pol_ang=}, which should not happen"
    return pol_ang


# TODO: Inline with other changing conventions this should be
# changed to `create_ms_summary`
def describe_ms(ms: MS | Path, verbose: bool = False) -> MSSummary:
    """Print some basic information from the inpute measurement set.

    Args:
        ms (Union[MS,Path]): Measurement set to inspect
        verbose (bool, optional): Log MS options to the flint logger. Defaults to False.

    Returns:
        MSSummary: Brief overview of the MS.

    """
    ms = MS(path=ms) if isinstance(ms, Path) else ms
    logger.info(f"Obtaining MSSummary for {ms.path}")

    with table(str(ms.path), readonly=True, ack=False) as tab:
        colnames = tab.colnames()

        flags: np.ndarray = tab.getcol("FLAG")
        flagged = np.sum(flags == True)  # Noqa: E712
        unflagged = np.sum(flags == False)  # Noqa: E712
        total = np.prod(flags.shape)
        flag_spectrum = flags.sum(axis=(0, -1)) / (flags.shape[0] * flags.shape[-1])

        uniq_ants = sorted(list(set(tab.getcol("ANTENNA1"))))

    with table(f"{ms.path}/FIELD", readonly=True, ack=False) as tab:
        uniq_fields = list(set(tab.getcol("NAME")))

    beam_no = get_beam_from_ms(ms=ms)
    phase_dir = get_phase_dir_from_ms(ms=ms)

    if verbose:
        logger.info(f"Inspecting {ms.path}.")
        logger.info(f"Contains: {colnames}")

        logger.info(f"{flagged} of {total} flagged ({flagged / total * 100.0:.4f}%). ")
        logger.info(f"{len(uniq_ants)} unique antenna: {uniq_ants}")
        logger.info(f"Unique fields: {uniq_fields}")
        logger.info(f"Phase direction: {phase_dir}")

    return MSSummary(
        flagged=flagged,
        unflagged=unflagged,
        flag_spectrum=flag_spectrum,
        fields=uniq_fields,
        ants=uniq_ants,
        beam=beam_no,
        path=ms.path,
        phase_dir=phase_dir,
    )


def split_by_field(
    ms: MS | Path,
    field: str | None = None,
    out_dir: Path | None = None,
    column: str | None = None,
) -> list[MS]:
    """Attempt to split an input measurement set up by the unique FIELDs recorded

    Args:
        ms (Union[MS, Path]): Input measurement sett to split into smaller MSs by field name
        field (Optional[str], optional): Desired field to extract. If None, all are split. Defaults to None.
        out_dir (Optional[Path], optional): Output directory to write the fresh MSs to. If None, write to same directory as
        parent MS. Defaults to None.
        column (Optional[str], optional): If not None, set the column attribute of the output MS instance to this. Defaults to None.

    Returns:
        List[MS]: The output MSs split by their field name.
    """
    ms = MS.cast(ms)

    # TODO: Split describe_ms up so can get just fields
    ms_summary = describe_ms(ms, verbose=False)

    logger.info("Collecting field names and corresponding FIELD_IDs")
    fields = [field] if field else ms_summary.fields
    field_idxs = [get_field_id_for_field(ms=ms, field_name=field) for field in fields]

    out_mss: list[MS] = []

    ms_out_dir: Path = Path(out_dir) if out_dir is not None else ms.path.parent
    logger.info(f"Will write output MSs to {ms_out_dir}.")

    if not ms_out_dir.exists():
        try:
            logger.info(f"Creating {ms_out_dir}.")
            ms_out_dir.mkdir(parents=True)
        except Exception as e:
            logger.warning(e)
            pass  # In case above fails due to race condition

    logger.info(f"Opening {ms.path}. ")
    with table(str(ms.path), ack=False) as tab:  # noqa: F841
        for split_name, split_idx in zip(fields, field_idxs):
            logger.info(f"Selecting FIELD={split_name}")
            sub_ms = taql(f"select * from $tab where FIELD_ID=={split_idx}")

            out_ms_str = create_ms_name(ms_path=ms.path, field=split_name)
            out_path = ms_out_dir / Path(out_ms_str).name

            logger.info(f"Writing {out_path!s} for {split_name}")
            sub_ms.copy(str(out_path), deep=True)

            out_mss.append(
                MS(path=out_path, beam=get_beam_from_ms(out_path), column=column)
            )

    return out_mss


def check_column_in_ms(
    ms: MS | str | PathLike,
    column: str | None = None,
    sub_table: str | None = None,
) -> bool:
    """Checks to see whether a column exists in an MS. If `column` is provided this
    is checked. It `column` is None, then the MS.column is specified. If both are
    `None` then an error is raised.

    Args:
        ms (Union[MS, str, PathLike]): The measurement set to check. Will attempt to cast to Path.
        column (Optional[str], optional): The column to check for. Defaults to None. sub_table (Optional[str], optional): A sub-table of the measurement set to inspect. If `None` the main table is examined. Defaults to None.

    Raises:
        ValueError: Raised when both `column` and `ms.column` are None.

    Returns:
        bool: Whether the column exists in the measurement set.
    """

    check_col = column
    if isinstance(ms, MS):
        logger.debug(f"{ms.column=} {column=}")
        check_col = column if column is not None else ms.column

    if check_col is None:
        raise ValueError(f"No column to check specified: {ms} {column=}.")

    ms_path = ms.path if isinstance(ms, MS) else Path(ms)
    check_table = str(ms_path) if sub_table is None else f"{ms_path!s}/{sub_table}"

    logger.debug(f"Checking for {check_col} in {check_table}")
    with table(check_table, readonly=True) as tab:
        tab_cols = tab.colnames()
        logger.debug(f"{ms_path} contains columns={tab_cols}.")
        result = check_col in tab_cols

    return result


def consistent_ms(ms1: MS, ms2: MS) -> bool:
    """Perform some basic consistency checks to ensure MS1 can
    be combined with MS2. This is important when considering
    candidate AO-style calibration solution files.

    Args:
        ms1 (MS): The first MS to consider
        ms2 (MS): The second MS to consider

    Returns:
        bool: Whether MS1 is consistent with MS2
    """

    logger.info(f"Comparing ms1={ms1.path!s} to ms2={(ms2.path)}")
    beam1 = get_beam_from_ms(ms=ms1)
    beam2 = get_beam_from_ms(ms=ms2)

    result = True
    reasons = []
    if beam1 != beam2:
        logger.debug(f"Beams are different: {beam1=} {beam2=}")
        reasons.append(f"{beam1=} != {beam2=}")
        result = False

    freqs1 = get_freqs_from_ms(ms=ms1)
    freqs2 = get_freqs_from_ms(ms=ms2)

    if len(freqs1) != len(freqs2):
        logger.debug(f"Length of frequencies differ: {len(freqs1)=} {len(freqs2)=}")
        reasons.append(f"{len(freqs1)=} != {len(freqs2)=}")
        result = False

    min_freqs1, min_freqs2 = np.min(freqs1), np.min(freqs2)
    if min_freqs1 != min_freqs2:
        logger.debug(f"Minimum frequency differ: {min_freqs1=} {min_freqs2=}")
        reasons.append(f"{min_freqs1=} != {min_freqs2=}")
        result = False

    max_freqs1, max_freqs2 = np.max(freqs1), np.max(freqs2)
    if min_freqs1 != min_freqs2:
        logger.debug(f"Maximum frequency differ: {max_freqs1=} {max_freqs2=}")
        reasons.append(f"{max_freqs1=} != {max_freqs2=}")
        result = False

    if not result:
        logger.info(f"{ms1.path!s} not compatibale with {ms2.path!s}, {reasons=}")

    return result


def consistent_channelwise_frequencies(
    freqs: list[np.ndarray] | np.ndarray,
) -> np.ndarray:
    """Given a collection of frequencies in the form of
    (N, frequencies), inspect the frequencies channelwise
    to ensure they are all the same.

    This does not operate on MSs, just the collection of frequencies

    Args:
        freqs (Union[List[np.ndarray], np.ndarray]): The collection of frequencies to be inspected

    Returns:
        np.ndarray: Same length as the frequencies. True if for a single channel all frequencies are the same. False otherwise.
    """
    freqs = np.array(freqs)
    assert len(freqs.shape) == 2, (
        f"{freqs.shape=}, but was expecting something of rank 2"
    )

    freqs_are_same = np.all(freqs - freqs[0, None] == 0, axis=1)
    assert len(freqs_are_same.shape) == 1, (
        f"Channelwise check should be length 1, but have {freqs_are_same.shaope=}"
    )
    return freqs_are_same


def consistent_ms_frequencies(mss: tuple[MS, ...]) -> bool:
    """Given a set of measurement sets, inspect the frequencies
    to ensure they are all the same

    See the ``get_freqs_from_ms`` function, which is used
    internally.

    Args:
        mss (Tuple[MS, ...]): Collection of MSs to inspect the frequencies of

    Returns:
        bool: Whether all the frequencies and their order are the same
    """

    logger.info(f"Collection frequencies from {len(mss)} measurement sets")
    freqs = [get_freqs_from_ms(ms=ms) for ms in mss]

    all_the_same = consistent_channelwise_frequencies(freqs=freqs)

    return np.all(all_the_same)


def rename_column_in_ms(
    ms: MS,
    original_column_name: str,
    new_column_name: str,
    update_tracked_column: bool = False,
) -> MS:
    """Rename a column in a measurement set. Optionally update the tracked
    `data` column attribute of the input measurement set.

    Args:
        ms (MS): Measurement set with the column to rename
        original_column_name (str): The name of the column that will be changed
        new_column_name (str): The new name of the column set in `original_column_name`
        update_tracked_column (bool, optional): Whether the `data` attribute of `ms` will be updated to `new_column_name`. Defaults to False.

    Returns:
        MS: The measurement set operated on
    """
    ms = MS.cast(ms=ms)

    with table(tablename=str(ms.path), readonly=False, ack=False) as tab:
        colnames = tab.colnames()
        assert original_column_name in colnames, (
            f"{original_column_name=} missing from {ms}"
        )
        assert new_column_name not in colnames, (
            f"{new_column_name=} already exists in {ms}"
        )

        logger.info(f"Renaming {original_column_name} to {new_column_name}")
        tab.renamecol(oldname=original_column_name, newname=new_column_name)
        tab.flush()

    if update_tracked_column:
        ms = ms.with_options(column=new_column_name)

    return ms


def remove_columns_from_ms(
    ms: MS | Path, columns_to_remove: str | list[str]
) -> list[str]:
    """Attempt to remove a collection of columns from a measurement set.
    If any of the provided columns do not exist they are ignored.

    Args:
        ms (Union[MS, Path]): The measurement set to inspect and remove columns from
        columns_to_remove (Union[str, List[str]]): Collection of column names to remove. If a single column internally it is cast to a list of length 1.

    Returns:
        List[str]: Collection of column names removed
    """

    if isinstance(columns_to_remove, str):
        columns_to_remove = [columns_to_remove]

    ms = MS.cast(ms=ms)
    with table(tablename=str(ms.path), readonly=False, ack=False) as tab:
        colnames = tab.colnames()
        columns_to_remove = [c for c in columns_to_remove if c in colnames]
        if len(columns_to_remove) == 0:
            logger.info(f"All columns provided do not exist in {ms.path}")
        else:
            logger.info(f"Removing {columns_to_remove=} from {ms.path}")
            tab.removecols(columnnames=columns_to_remove)

    return columns_to_remove


def subtract_model_from_data_column(
    ms: MS,
    model_column: str = "MODEL_DATA",
    data_column: str | None = None,
    output_column: str | None = None,
    update_tracked_column: bool = False,
) -> MS:
    """Execute a ``taql`` query to subtract the MODEL_DATA from a nominated data column.
    This requires the ``model_column`` to already be inserted into the MS. Internally
    the ``critical_ms_interaction`` context manager is used to highlight that the MS
    is being modified should things fail when subtracting.

    Args:
        ms (MS): The measurement set instance being considered
        model_column (str, optional): The column with representing the model. Defaults to "MODEL_DATA".
        data_column (Optional[str], optional): The column where the column will be subtracted. If ``None`` it is taken from the ``column`` nominated by the input ``MS`` instance. Defaults to None.
        output_column (Optional[str], optional): The output column that will be created. If ``None`` it defaults to ``data_column``. Defaults to None.
        update_tracked_column (bool, optional): If True, update ``ms.column`` to the column with subtracted data. Defaults to False.

    Returns:
        MS: The updated MS
    """
    ms = MS.cast(ms)
    data_column = data_column if data_column else ms.column
    assert data_column is not None, f"{data_column=}, which is not allowed"

    output_column = output_column if output_column else data_column
    assert output_column is not None, f"{output_column=}, which is not allowed"
    with critical_ms_interaction(input_ms=ms.path) as critical_ms:
        with table(str(critical_ms), readonly=False) as tab:
            logger.info("Extracting columns")
            colnames = tab.colnames()
            assert all([d in colnames for d in (model_column, data_column)]), (
                f"{model_column=} or {data_column=} missing from {colnames=}"
            )

            if output_column not in colnames:
                from casacore.tables import makecoldesc

                logger.info(f"Adding {output_column=}")
                desc = makecoldesc(data_column, tab.getcoldesc(data_column))
                desc["name"] = output_column
                tab.addcols(desc)
                tab.flush()

            logger.info(f"Subtracting {model_column=} from {data_column=}")
            taql(f"UPDATE $tab SET {output_column}={data_column}-{model_column}")

    if update_tracked_column:
        logger.info(f"Updating ms.column to {output_column=}")
        ms = ms.with_options(column=output_column)
    return ms


# TODO: Clean up the usage and description of the argument `instrument_column`
# as it is currently being used in unclear ways. Specifically there is a renaming
# of the data_column to instrument_column before the rotation of things
def preprocess_askap_ms(
    ms: MS | Path,
    data_column: str = "DATA",
    instrument_column: str = "INSTRUMENT_DATA",
    overwrite: bool = True,
    skip_rotation: bool = False,
    fix_stokes_factor: bool = False,
    apply_ms_transform: bool = False,
    casa_container: Path | None = None,
    rename: bool = False,
) -> MS:
    """The ASKAP MS stores its data in a way that is not immediately accessible
    to other astronomical software, like wsclean or casa. For each measurement set
    the centre of the field is stored, and beam offsets are stored in a separate table.

    Additionally, the correlations stored are more akin to (P, Q) -- they are not
    (X, Y) in the sky reference frame. This function does two things:

    * updates the positions stored so when data are imaged/calibrated the correlations are directed to the correct position
    * will apply a rotation to go from (P, Q) -> (X, Y)

    These corrections are applied to the original MS, and should be
    able to be executed multiple times without accumulating changes.

    Args:
        ms (Union[MS, Path]): The measurement set to update
        data_column (str, optional): The name of the data column to correct. This will first be renamed to the value specified by `instrument_column` before being corrected. Defaults to 'DATA'.
        instrument_column (str, optional): The name of the column that will hold the original `data_column` data. Defaults to 'INSTRUMENT_DATA'
        overwrite (bool, optional): If the `instrument_column` and `data_column` both exist and `overwrite=True` the `data_column` will be overwritten. Otherwise, a `ValueError` is raised. Defaults to True.
        skip_rotation (bool, optional): If true, the visibilities are not rotated Defaults to False.
        fix_stokes_factor (bool, optional): Apply the stokes scaling factor (aruses in different definition of Stokes between Ynadasoft and other applications) when rotation visibilities. This should be set to False is the bandpass solutions have already absorded this scaling term. Defaults to False.
        apply_ms_transform (bool, optional): If True, the MS will be transformed using the `casa_container` provided. Defaults to False.
        casa_container (Path, optional): The path to the CASA container that will be used to transform the MS. Defaults to None.
        rename (bool, optional): If True, the MS will be renamed to a Flint-processed name. Defaults to False.

    Returns:
        MS: An updated measurement set with the corrections applied.
    """
    ms = MS.cast(ms)

    assert data_column != instrument_column, (
        f"Received matching column names: {data_column=} {instrument_column=}"
    )

    if apply_ms_transform:
        if casa_container is None:
            raise ValueError(
                "apply_ms_transform=True, but no casa_container provided. "
            )
        corrected_ms_path = copy_with_mstranform(
            casa_container=casa_container,
            ms_path=ms.path,
        )
        ms = MS.cast(corrected_ms_path)

    logger.info(f"Will be running ASKAP MS conversion operations against {ms.path!s}.")
    logger.info("Correcting directions. ")

    with table(str(ms.path), ack=False, readonly=False) as tab:
        colnames = tab.colnames()
        if data_column not in colnames:
            raise ValueError(
                f"Column {data_column} not found in {ms.path!s}. Columns found: {colnames}"
            )
        if all([col in colnames for col in (data_column, instrument_column)]):
            msg = (
                f"Column {instrument_column} already in {ms.path!s}. Already corrected?"
            )
            if not overwrite:
                raise ValueError(msg)

        if (
            not skip_rotation
            and data_column in colnames
            and instrument_column not in colnames
        ):
            logger.info(f"Renaming {data_column} to {instrument_column}.")
            tab.renamecol(data_column, instrument_column)

        tab.flush()

    logger.info("Correcting the field table. ")
    fix_ms_dir(ms=str(ms.path))

    if skip_rotation:
        # TODO: Should we copy the DATA to INSTRUMENT_DATA?
        logger.info("Skipping the rotation of the visibilities. ")
        ms = ms.with_options(column=data_column)
        logger.info(f"Returning {ms=}.")
        return ms

    logger.info("Applying rotation matrix to correlations. ")
    logger.info(
        f"Rotating visibilities for {ms.path} with data_column={instrument_column} and corrected_data_column={data_column}"
    )
    fix_ms_corrs(
        ms=ms.path,
        data_column=instrument_column,
        corrected_data_column=data_column,
        fix_stokes_factor=fix_stokes_factor,
    )

    if rename:
        logger.info("Renaming the measurement set.")
        new_ms_name_str = create_ms_name(ms_path=ms.path)
        new_ms_path = ms.path.parent / Path(new_ms_name_str)
        shutil.move(ms.path, new_ms_path)
        ms = ms.with_options(path=new_ms_path)

    return ms.with_options(column=data_column)


def copy_and_preprocess_casda_askap_ms(
    casda_ms: MS | Path,
    data_column: str = "DATA",
    instrument_column: str = "INSTRUMENT_DATA",
    fix_stokes_factor: bool = True,
    output_directory: Path = Path("./"),
) -> MS:
    """Convert an ASKAP pipeline MS from CASDA into a FLINT form. This involves
    making a copy of it, updating its name, and then preprocessing.

    Typically these MSs are:

    - bandpass calibrated
    - self-calibrated
    - in the instrument frame
    - has factor of 2 scaling

    This function attempts to craft the MS so that the data column has had visibilities rotated
    and scaled to make them compatible with certain imaging packages (e.g. wsclean).

    Args:
        casda_ms (Union[MS, Path]): The measurement set to preprocess
        data_column (str, optional): The column with data to preprocess. Defaults to "DATA".
        instrument_column (str, optional): The name of the column to be created with data in the instrument frame. Defaults to "INSTRUMENT_DATA".
        fix_stokes_factor (bool, optional): Whether to scale the visibilities to account for the factor of 2 error. Defaults to True.
        output_directory (Path, optional): The output directory that the preprocessed MS will be placed into. Defaults to Path("./").

    Returns:
        MS: a corrected and preprocessed measurement set
    """
    ms = MS.cast(casda_ms)

    out_ms_path = output_directory / create_ms_name(ms_path=ms.path)
    logger.info(f"New MS name: {out_ms_path}")
    out_ms_path = copy_directory(input_directory=ms.path, output_directory=out_ms_path)

    ms = ms.with_options(path=out_ms_path)

    logger.info(
        f"Will be running CASDA ASKAP MS conversion operations against {ms.path!s}."
    )

    with table(str(ms.path), ack=False, readonly=False) as tab:
        column_names = tab.colnames()
        assert data_column in column_names and instrument_column not in column_names, (
            f"{ms.path} column names failed. {data_column=} {instrument_column=} {column_names=}"
        )
        tab.renamecol(data_column, instrument_column)
        tab.flush()

    logger.info("Correcting directions. ")
    fix_ms_dir(ms=str(ms.path))

    logger.info("Applying rotation matrix to correlations. ")
    logger.info(
        f"Rotating visibilities for {ms.path} with data_column={instrument_column} and corrected_data_column={data_column}"
    )
    fix_ms_corrs(
        ms=ms.path,
        data_column=instrument_column,
        corrected_data_column=data_column,
        fix_stokes_factor=fix_stokes_factor,
    )

    return ms.with_options(column=data_column)


def rename_ms_and_columns_for_selfcal(
    ms: MS,
    target: str | Path,
    corrected_data: str = "CORRECTED_DATA",
    data: str = "DATA",
) -> MS:
    """Take an existing measurement set, rename it, and appropriately
    rename the "DATA" and "CORRECTED_DATA" columns to support a new
    round of imaging and self-calibration.

    This could be considered for larger measurement sets where holding
    multiple copies throughout rounds of self-calibration is not advisable.

    Args:
        ms (MS): The subject measurement set to rename
        target (Union[str, Path]): The targett path the measurement set will be renamed to. This should not already exist.
        corrected_data (str, optional): The name of the column with the latest calibrated data. This becomes the `data` column. Defaults to "CORRECTED_DATA".
        data (str, optional): The name of the column that will be subsequently processed. If it exists it will be removed. Defaults to "DATA".

    Raises:
        FileExistsError: Raised if `target` already exists
        FileNotFoundError: Raise if `ms.path` does not exist

    Returns:
        MS: Updated MS container with new path and appropriate data column
    """

    ms = MS.cast(ms)
    target = Path(target)

    if target.exists():
        raise FileExistsError(f"{target} already exists!")
    if not ms.path.exists() or not ms.path.is_dir():
        raise FileNotFoundError(
            f"{ms.path} does not exists or is not a directory (hence measurement set)."
        )

    logger.info(f"Renaming {ms.path} to {target=}")
    ms.path.rename(target=target)

    # Just some sanity in case None is passed through
    if not (corrected_data or data):
        return ms.with_options(path=target)

    # Now move the corrected data column into the column to be imaged.
    # For casa it really needs to be DATA
    with table(str(target), readonly=False, ack=False) as tab:
        colnames = tab.colnames()

        if (
            all([col in colnames for col in (corrected_data, data)])
            and corrected_data != data
        ):
            logger.info(f"Removing {data} and renaming {corrected_data}")
            tab.removecols(columnnames=data)
            tab.renamecol(oldname=corrected_data, newname=data)
        elif (
            corrected_data in colnames
            and data not in controlnames
            and corrected_data != data
        ):
            logger.info(f"Renaming {corrected_data=} to {data=}")
            tab.renamecol(oldname=corrected_data, newname=data)
        elif corrected_data not in colnames and data in colnames:
            logger.warning(f"No {corrected_data=}, and {data=} seems to be present")
        elif (
            all([col in colnames for col in (corrected_data, data)])
            and corrected_data == data
            and corrected_data != "DATA"
        ):
            data = "DATA"
            logger.info(f"Renaming {corrected_data} to DATA")
            tab.renamecol(corrected_data, data)

        tab.flush()

    # This is a safe guard against my bad handling of the above / mutineers
    # There could be interplay with these columns when potato peel is used
    # as some MSs will have CORRECYED_DATA and others may not.
    assert data == "DATA", (
        f"Somehow data column is not DATA, instead {data=}. Likely a problem for casa."
    )

    return ms.with_options(path=target, column=data)


def find_mss(
    mss_parent_path: Path,
    expected_ms_count: int | None = 36,
    data_column: str | None = None,
    model_column: str | None = None,
) -> tuple[MS, ...]:
    """Search a directory to find measurement sets via a simple
    `*.ms` glob expression. An expected number of MSs can be enforced
    via the `expected_ms_count` option.

    Args:
        mss_parent_path (Path): The parent directory that will be globbed to search for MSs.
        expected_ms_count (Optional[int], optional): The number of MSs that should be there. If None no check is performed. Defaults to 36.
        data_column (Optional[str], optional): Set the column attribute of each MS to this (no checks to ensure it exists). If None use default of MS. Defaults to None.
        model_column (Optional[str], optional): Set the model column attribute of each MS to this (no checks to ensure it exists). If None use default of MS. Defaults to None.

    Returns:
        Tuple[MS, ...]: Collection of found MSs
    """
    assert mss_parent_path.exists() and mss_parent_path.is_dir(), (
        f"{mss_parent_path!s} does not exist or is not a folder. "
    )

    found_mss = tuple(
        [MS.cast(ms_path) for ms_path in sorted(mss_parent_path.glob("*.ms"))]
    )

    if expected_ms_count:
        assert len(found_mss) == expected_ms_count, (
            f"Expected to find {expected_ms_count} in {mss_parent_path!s}, found {len(found_mss)}."
        )

    if data_column or model_column:
        logger.info(f"Updating column attribute to {data_column=}")
        found_mss = tuple(
            [
                found_ms.with_options(column=data_column, model_column=model_column)
                for found_ms in found_mss
            ]
        )

    return found_mss


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Components to interact with MS")

    subparser = parser.add_subparsers(dest="mode")

    split_parser = subparser.add_parser(
        "split", help="Split an MS based on field name. "
    )

    split_parser.add_argument("ms", type=Path, help="MS to split based on fields. ")
    split_parser.add_argument(
        "--ms-out-dir",
        type=Path,
        default=None,
        help="Location to write the output MSs to. ",
    )

    preprocess_parser = subparser.add_parser(
        "preprocess",
        help="Apply preprocessing operations to the ASKAP MS so it can be used outside of yandasoft",
    )

    preprocess_parser.add_argument("ms", type=Path, help="Measurement set to correct. ")

    compatible_parser = subparser.add_parser(
        "compatible", help="Some basic checks to ensure ms1 is consistent with ms2."
    )
    compatible_parser.add_argument(
        "ms1", type=Path, help="The first measurement set to consider. "
    )
    compatible_parser.add_argument(
        "ms2", type=Path, help="The second measurement set to consider. "
    )

    casda_parser = subparser.add_parser(
        "casda",
        help="Apply preprocessing operations to the CASDA ASKAP pipeline MS so it can be used outside of yandasoft",
    )

    casda_parser.add_argument(
        "casda_ms",
        type=Path,
        help="Path to the ASKAP pipeline produced MS obtained through casda",
    )
    casda_parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("./"),
        help="Directory to write the new FLINT MS to",
    )

    return parser


def cli() -> None:
    import logging

    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "split":
        split_by_field(ms=args.ms, out_dir=args.ms_out_dir)
    if args.mode == "preprocess":
        preprocess_askap_ms(ms=args.ms)
    if args.mode == "compatible":
        res = consistent_ms(ms1=MS(path=args.ms1), ms2=MS(path=args.ms2))
        if res:
            logger.info(f"{args.ms1} is compatible with {args.ms2}")
        else:
            logger.info(f"{args.ms1} is not compatible with {args.ms2}")
    if args.mode == "casda":
        copy_and_preprocess_casda_askap_ms(
            casda_ms=Path(args.casda_ms), output_directory=Path(args.output_directory)
        )


if __name__ == "__main__":
    cli()
