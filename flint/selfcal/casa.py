"""Utilities related to using casa tasks to perform self-calibration.

This tooling is mostly centred on using gaincal from casatasks.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from casacore.tables import table

from flint.casa import applycal, gaincal
from flint.exceptions import GainCalError, MSError
from flint.logging import logger
from flint.ms import check_column_in_ms
from flint.options import MS, BaseOptions
from flint.selfcal.utils import (
    create_and_check_caltable_path,
    get_channel_ranges_given_nspws_for_ms,
    prepare_ms_for_selfcal,
)
from flint.utils import zip_folder


class GainCalOptions(BaseOptions):
    """Options provided to the casatasks gaincal function. Most options correspond to those in gaincal."""

    solint: str = "60s"
    """Solution length interval"""
    calmode: str = "p"
    """Calibration mode. """
    round: int = 0
    """Self-calibration round. Not a gaincal option. """
    minsnr: float = 0.0
    """Minimum signal-to-noise of the solutions. Below this the solutions and data are flagged. """
    uvrange: str = ">200m"
    """Data selected to go through calibration procedure"""
    selectdata: bool = True
    """Whether data selection actions will be applied in gaincal. """
    gaintype: str = "G"
    """The gain type that would be solved for. """
    nspw: int = 1
    """The number of spectral windows to use during the self-calibration routine. This
    will be used to craft an appropriate ``select_spw=`` interval range. If larger
    than one, ``gaincal`` will be carried out against each interval and results will
    be appended to a common solutions file. """
    solnorm: bool = True
    """Renormalise the distribution of the amplite gains so the mean is unity."""


def check_for_valid_calibration_table(caltable_path: Path) -> bool:
    """Basic checks to ensure that the calibration table
    from CASAs gaincal is valid

    Args:
        caltable_path (Path): Table path to examine

    Returns:
        bool: Whether the input table appears to be a CASA solutions table from gaincal
    """

    if not caltable_path.exists() or not caltable_path.is_dir():
        return False

    logger.info(f"{caltable_path=}")

    try:
        with table(str(caltable_path)) as tab:
            colnames = tab.colnames()
            no_rows = len(tab)
    except:  # noqa: E722
        return False

    expected_columns = [
        "TIME",
        "FIELD_ID",
        "SPECTRAL_WINDOW_ID",
        "ANTENNA1",
        "ANTENNA2",
        "INTERVAL",
        "SCAN_NUMBER",
        "OBSERVATION_ID",
        "CPARAM",
        "PARAMERR",
        "FLAG",
        "SNR",
        "WEIGHT",
    ]

    all_columns_exist = all(col in colnames for col in expected_columns)

    return no_rows >= 1 and all_columns_exist


def _process_gaincal_options(
    gain_cal_options: GainCalOptions | None = None,
    update_gain_cal_options: dict[str, Any] | None = None,
) -> GainCalOptions:
    """Helper to process and update GaincalOptions on initialise of self-calibration routine

    Args:
        gain_cal_options (GainCalOptions | None, optional): Any existing gaincal options. Defaults to None.
        update_gain_cal_options (dict[str, Any] | None, optional): Overrides to provide the to input gaincal options or defaults. Defaults to None.

    Returns:
        GainCalOptions: options to use throughout gaincalibtaion
    """
    if gain_cal_options is None:
        gain_cal_options = GainCalOptions()
    if update_gain_cal_options:
        logger.info(f"Updating gaincal options with: {update_gain_cal_options}")
        gain_cal_options = gain_cal_options.with_options(**update_gain_cal_options)

    return gain_cal_options


def gaincal_applycal_ms(
    ms: MS,
    casa_container: Path,
    gain_cal_options: GainCalOptions | None = None,
    update_gain_cal_options: dict[str, Any] | None = None,
) -> MS:
    """Self-calibrate the DATA column of a measurement set into CORRECTED_DATA
    using casa's gaincal and applycal tasks. See ``prepare_ms_for_selfcal``.

    Args:
        ms (MS): Measurement set that will be self-calibrated. Its column must be DATA.
        casa_container (Path): A path to a singularity container with CASA tooling.
        gain_cal_options (Optional[GainCalOptions], optional): Options provided to gaincal. Defaults to None.
        update_gain_cal_options (Optional[Dict[str, Any]], optional): Update the gain_cal_options with these. Defaults to None.

    Raises:
        MSError: Raised when the column of ``ms`` is not DATA
        GainCallError: Raised when no self-calibration solutions were found

    Returns:
        MS: The self-calibrated measurement set.
    """
    # This function tries to ensure side-effects from failed attempts are recoverable
    # which sometimes happens in some HPC environments. This is best effort. In summary
    # if an existing calibration table exists, gain cal is not performed.
    # The tables can be applied from DATA -> CORRECTED_DATA any number of times
    # without any negative effects (other than overwriting CORRECTED_DATA). The
    # final archiving of tables as the last step means the applysolutions can
    # successfully find the tables.

    logger.info(f"Measurement set to be self-calibrated: ms={ms}")

    assert casa_container.exists(), f"{casa_container=} does not exist. "

    if ms.column != "DATA":
        raise MSError(f"casa calibrates DATA into CORRECTED_DATA, but {ms.column=}")

    gain_cal_options = _process_gaincal_options(
        gain_cal_options=gain_cal_options,
        update_gain_cal_options=update_gain_cal_options,
    )

    # First, we collect the solutions for each of the requested SPW
    spw_and_cal_tables = []
    channel_ranges = get_channel_ranges_given_nspws_for_ms(
        ms=ms, nspw=gain_cal_options.nspw
    )
    for idx, channel_range in enumerate(channel_ranges):
        logger.info(f"Calibrating {idx + 1} of {len(channel_ranges)}, {channel_range=}")
        spw_str = f"0:{channel_range[0]}~{channel_range[1]}"
        cal_table = create_and_check_caltable_path(ms=ms, channel_range=channel_range)

        # This check attempts to avoid reduplication of work if already
        # carried out. Trying to make this function pure with no side
        # effects, matie
        if check_for_valid_calibration_table(caltable_path=cal_table):
            logger.warning(f"Found an earlier {cal_table=}. Not rerunning gaincal!")
        else:
            gaincal(
                container=casa_container,
                bind_dirs=(ms.path.parent, cal_table.parent),
                vis=str(ms.path),
                caltable=str(cal_table),
                spw=spw_str,
                solint=gain_cal_options.solint,
                gaintype=gain_cal_options.gaintype,
                minsnr=gain_cal_options.minsnr,
                calmode=gain_cal_options.calmode,
                selectdata=gain_cal_options.selectdata,
                uvrange=gain_cal_options.uvrange,
                solnorm=gain_cal_options.solnorm,
            )

        if not cal_table.exists():
            logger.critical(
                "The calibration table was not created. Likely gaincal failed. "
            )
            raise GainCalError(f"Gaincal failed for {ms.path}")

        spw_and_cal_tables.append((spw_str, cal_table))

    # Now apply each of the solutions to the corresponding SPW.
    # Relying on the spw= channel selection to only be updating
    # the visibilities in the existing CORRECTED_DATA column,
    # not overwriting the entire column
    for idx, (spw_str, cal_table) in enumerate(spw_and_cal_tables):
        # shoulod this be rerunning in again, from recovery of a
        # dead worker, then this will simply reapply the solutions
        # overwriting any that may already existing in the column.
        # Some operations are lost, but no side effects, ya sea dog
        logger.info(
            f"Apply solutions {idx + 1} of {len(spw_and_cal_tables)}, {spw_str}"
        )
        applycal(
            container=casa_container,
            bind_dirs=(ms.path.parent, cal_table.parent),
            vis=str(ms.path),
            gaintable=str(cal_table),
            spw=spw_str,
            flagbackup=False,
        )

    # Only archive tables if application of solutions was successful. If
    # we ought to make sure we don't potentially double up on work
    logger.info("Archiving calibration tables")
    for _, cal_table in spw_and_cal_tables:
        zip_folder(in_path=cal_table)

    flag_versions_table = ms.path.with_suffix(".ms.flagversions")
    if flag_versions_table.exists():
        zip_folder(in_path=flag_versions_table)

    return ms.with_options(column="CORRECTED_DATA")


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    sub_parsers = parser.add_subparsers(dest="mode")

    gaincal_parser = sub_parsers.add_parser("gaincal")
    gaincal_parser.add_argument(
        "ms",
        type=Path,
        help="Path to the measurement set to calibrate. It is renamed for the round, not copied",
    )
    gaincal_parser.add_argument(
        "--casa-container",
        type=Path,
        required=True,
        help="Path to the CASA6 singularity container",
    )
    gaincal_parser.add_argument(
        "--round", type=int, default=1, help="Self-calibration round number. "
    )
    gaincal_parser.add_argument(
        "--column",
        type=str,
        default="DATA",
        help="The column to self-calibrate. It is moved into DATA",
    )
    gaincal_parser.add_argument(
        "--keep-old-columns",
        action="store_true",
        help="Keep the replaced DATA column rather than removing it",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "gaincal":
        if not args.casa_container.exists():
            raise FileNotFoundError(f"{args.casa_container=} does not exist")
        # applycal would overwrite a previous round's CORRECTED_DATA. Not checked in
        # prepare_ms_for_selfcal, as peeling in the flows leaves one beside DATA
        if args.column == "DATA" and check_column_in_ms(
            ms=args.ms, column="CORRECTED_DATA"
        ):
            raise MSError(
                f"{args.ms} has CORRECTED_DATA, which applycal would overwrite. "
                "Pass --column CORRECTED_DATA to calibrate it, or remove it first"
            )
        ms = prepare_ms_for_selfcal(
            ms=MS(path=args.ms, column=args.column),
            round=args.round,
            keep_old_columns=args.keep_old_columns,
        )
        gaincal_applycal_ms(ms=ms, casa_container=args.casa_container)


if __name__ == "__main__":
    cli()
