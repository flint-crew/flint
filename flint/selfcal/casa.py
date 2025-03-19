"""Utilities related to using casa tasks to perform self-calibration.

This tooling is mostly centred on using gaincal from casatasks.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from shutil import copytree
from typing import Any

from casacore.tables import table

from flint.casa import applycal, cvel, gaincal, mstransform
from flint.exceptions import GainCalError, MSError
from flint.flagging import nan_zero_extreme_flag_ms
from flint.logging import logger
from flint.ms import rename_ms_and_columns_for_selfcal
from flint.naming import get_selfcal_ms_name
from flint.options import MS, BaseOptions
from flint.selfcal.utils import (
    create_and_check_caltable_path,
    get_channel_ranges_given_nspws_for_ms,
)
from flint.utils import remove_files_folders, rsync_copy_directory, zip_folder


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


def copy_and_clean_ms_casagain(
    ms: MS, round: int = 1, verify: bool = True, rename_ms: bool = False
) -> MS:
    """Create a copy of a measurement set in preparation for selfcalibration
    using casa's gaincal and applycal. Applycal only works when calibrating
    DATA and creating a CORRECTED_DATA column. Columns are removed in the
    copied MS to allow this.

    If the MS is large the `move_ms` option will simply renamed the input MS, adjusting
    the columns appropriately. Note that this potentially involves deleting the `DATA`
    column if present and renaming `CORRECTED_DATA`.

    Args:
        ms (MS): Measurement set that would go through self-calibration.
        round (int, optional): The self-calibration round. Defaults to 1.
        verify (bool, optional): Verify that copying the measurementt set (done in preparation for gaincal) was successful. Uses a call to rsync. Defaults to True.
        move_ms (bool, optional): Rather than copying the MS, simple renamed the MS and modify columns appropriately. Defaults to False.

    Returns:
        MS: Copy of input measurement set with columns removed as required.
    """
    out_ms_path = get_selfcal_ms_name(in_ms_path=ms.path, round=round)

    mode_text = "Renaming" if rename_ms else "Copying"

    logger.info(f"Output MS name will be {out_ms_path!s}.")
    logger.info(f"{mode_text} {ms.path} to {out_ms_path}.")

    if out_ms_path.exists():
        logger.warning(f"{out_ms_path} already exists. Removing it. ")
        logger.warning(f"{ms.path=} {ms.path.exists()=}")
        logger.warning(f"{out_ms_path=} {out_ms_path.exists()=}")
        remove_files_folders(out_ms_path)

    if rename_ms:
        if not ms.column:
            raise MSError(f"No column has been assigned: {ms}")

        ms = rename_ms_and_columns_for_selfcal(
            ms=ms, target=out_ms_path, corrected_data=ms.column, data="DATA"
        )
    else:
        copytree(ms.path, out_ms_path)
        # Because we can trust nothing, verify the
        # copy with rsync. On some lustre file systems (mostly seen on stonix)
        # components of the measurement sett are not always successfully
        # copied with a simple copytree.
        if verify:
            rsync_copy_directory(ms.path, out_ms_path)

        logger.info("Copying finished. ")

        # The casa gaincal and applycal tasks __really__ expect the input and output
        # column names to be DATA and CORRECTED_DATA. So, here we will go through the
        # motions of deleting and rnaming columns. Note that the MODEL_DATA column needs
        # to exist. The INSTRUMENT_DATA column will also be removed.
        logger.info("About to open the table. ")
        with table(str(out_ms_path), readonly=False, ack=False) as tab:
            logger.info("About to get the colnames")
            colnames = tab.colnames()
            logger.info(f"Column names are: {colnames}")
            if ms.column == "DATA" and "CORRECTED_DATA" not in colnames:
                logger.info(
                    "Data is the nominated column, and CORRECTED_DATA does not exist. Returning. "
                )
            else:
                to_delete = [
                    "DATA",
                ]
                for col in to_delete:
                    if col in colnames:
                        logger.info(f"Removing {col=} from {out_ms_path!s}.")
                        try:
                            tab.removecols(col)
                            tab.flush(recursive=True)
                        except Exception as e:
                            logger.critical(
                                f"Failed to remove {col=}! \nCaptured error: {e}"
                            )
                    else:
                        logger.warning(f"Column {col} not found in {out_ms_path!s}.")

                logger.info("Renaming CORRECTED_DATA to DATA. ")
                tab.renamecol("CORRECTED_DATA", "DATA")

            tab.flush()

    # Note that the out_ms_path needs to be set, even if the data  column is initially DATA.
    # Since casa expects DATA, we will force the column to be DATA with the expectation that
    # previous pirates in the lines above have dealt with the renaming.
    ms = ms.with_options(path=out_ms_path, column="DATA")

    ms = nan_zero_extreme_flag_ms(ms=ms)

    return ms


def create_spws_in_ms(casa_container: Path, ms_path: Path, nspw: int) -> Path:
    """Use the casa task mstransform to create `nspw` spectral windows
    in the input measurement set. This is necessary when attempting to
    use gaincal to solve for some frequency-dependent solution.

    Args:
        casa_container (Path): Path to the singularity container with CASA tooling
        ms_path (Path): The measurement set that should be reformed to have `nspw` spectral windows
        nspw (int): The number of spectral windows to create

    Returns:
        Path: The path to the measurement set that was updated
    """

    logger.info(f"Transforming {ms_path!s} to have {nspw} SPWs")
    transform_ms = ms_path.with_suffix(".ms_transform")

    mstransform(
        container=casa_container,
        bind_dirs=(ms_path.parent, transform_ms.parent),
        vis=str(ms_path),
        outputvis=str(transform_ms),
        regridms=True,
        nspw=nspw,
        mode="channel",
        nchan=-1,
        start=0,
        width=1,
        chanbin=1,
        createmms=False,
        datacolumn="all",
        combinespws=False,
    )

    logger.info(
        f"Successfully created the transformed measurement set {transform_ms} with {nspw} SPWs."
    )
    remove_files_folders(ms_path)

    logger.info(f"Renaming {transform_ms} to {ms_path}.")
    transform_ms.rename(ms_path)

    # Look above - we have renamed the cvel measurement set Captain
    return ms_path


def merge_spws_in_ms(casa_container: Path, ms_path: Path) -> Path:
    """Attempt to merge together all SPWs in the input measurement
    set using the `cvel` casa task. This can be a little fragile.

    The `cvel` task creates a new measurement set, so there would
    temporially be a secondary measurement set.

    Args:
        ms_path (Path): The measurement set that should have its SPWs merged together.

    Returns:
        Path: The measurement set with merged SPWs. It will have the same path as `ms_path`, but is a new measurement set.
    """
    logger.info("Will attempt to merge all spws using cvel.")

    cvel_ms_path = ms_path.with_suffix(".cvel")
    cvel(
        container=casa_container,
        bind_dirs=(ms_path.parent,),
        vis=str(ms_path),
        outputvis=str(cvel_ms_path),
        mode="channel_b",
    )

    logger.info(f"Successfully merged spws in {cvel_ms_path}")

    remove_files_folders(ms_path)

    logger.info(f"Renaming {cvel_ms_path} to {ms_path}")
    cvel_ms_path.rename(ms_path)

    # Look above - we have renamed the cvel measurement set Captain
    return ms_path


def gaincal_applycal_ms(
    ms: MS,
    casa_container: Path,
    round: int = 1,
    gain_cal_options: GainCalOptions | None = None,
    update_gain_cal_options: dict[str, Any] | None = None,
    archive_input_ms: bool = False,
    raise_error_on_fail: bool = True,
    skip_selfcal: bool = False,
    rename_ms: bool = False,
    archive_cal_table: bool = False,
) -> MS:
    """Perform self-calibration using casa's gaincal and applycal tasks against
    an input measurement set.

    Args:
        ms (MS): Measurement set that will be self-calibrated.
        round (int, optional): Round of self-calibration, which is used for unique names. Defaults to 1.
        casa_container (Path): A path to a singularity container with CASA tooling.
        gain_cal_options (Optional[GainCalOptions], optional): Options provided to gaincal. Defaults to None.
        update_gain_cal_options (Optional[Dict[str, Any]], optional): Update the gain_cal_options with these. Defaults to None.
        archive_input_ms (bool, optional): If True, the input measurement set will be compressed into a single file. Defaults to False.
        raise_error_on_fail (bool, optional): If gaincal does not converge raise en error. If False and gain cal fails return the input ms. Defaults to True.
        skip_selfcal (bool, optional): Should this self-cal be skipped. If `True`, the a new MS is created but not calibrated the appropriate new name and returned.
        rename_ms (bool, optional): It `True` simply rename a MS and adjust columns appropriately (potentially deleting them) instead of copying the complete MS. If `True` `archive_input_ms` is ignored. Defaults to False.
        archive_cal_table (bool, optional): Archive the output calibration table in a tarball. Defaults to False.

    Raises:
        GainCallError: Raised when raise_error_on_fail is True and gaincal does not converge.

    Returns:
        MS: The self-calibrated measurement set.
    """
    logger.info(f"Measurement set to be self-calibrated: ms={ms}")

    assert casa_container.exists(), f"{casa_container=} does not exist. "

    if gain_cal_options is None:
        gain_cal_options = GainCalOptions()
    if update_gain_cal_options:
        logger.info(f"Updating gaincal options with: {update_gain_cal_options}")
        gain_cal_options = gain_cal_options.with_options(**update_gain_cal_options)

    # TODO: If the skip_selfcal is True we should just symlink, maybe?
    # Pirates like easy things though.
    cal_ms = copy_and_clean_ms_casagain(ms=ms, round=round, rename_ms=rename_ms)

    # Archive straight after copying in case we skip the gaincal and return
    if archive_input_ms and not rename_ms:
        zip_folder(in_path=ms.path)

    # No need to do work me, hardy
    if skip_selfcal:
        logger.info(f"{skip_selfcal=}, not calibrating the MS. ")
        return cal_ms

    # First, we collect the solutions for each of the requested SPW
    spw_and_cal_tables = []
    channel_ranges = get_channel_ranges_given_nspws_for_ms(
        ms=cal_ms, nspw=gain_cal_options.nspw
    )
    for idx, channel_range in enumerate(channel_ranges):
        logger.info(f"Calibrating {idx + 1} of {len(channel_ranges)}, {channel_range=}")
        spw_str = f"0:{channel_range[0]}~{channel_range[1]}"
        cal_table = create_and_check_caltable_path(
            ms=cal_ms, channel_range=channel_range
        )

        gaincal(
            container=casa_container,
            bind_dirs=(cal_ms.path.parent, cal_table.parent),
            vis=str(cal_ms.path),
            caltable=str(cal_table),
            spw=spw_str,
            solint=gain_cal_options.solint,
            gaintype=gain_cal_options.gaintype,
            minsnr=gain_cal_options.minsnr,
            calmode=gain_cal_options.calmode,
            selectdata=gain_cal_options.selectdata,
            uvrange=gain_cal_options.uvrange,
        )

        if not cal_table.exists():
            logger.critical(
                "The calibration table was not created. Likely gaincal failed. "
            )
            if raise_error_on_fail:
                raise GainCalError(f"Gaincal failed for {cal_ms.path}")
            else:
                return ms

        spw_and_cal_tables.append((spw_str, cal_table))

    # Now apply each of the solutions to the corresponding SPW.
    # Relying on the spw= channel selection to only be updating
    # the visibilities in the existing CORRECTED_DATA column,
    # not overwriting the entire column
    for idx, (spw_str, cal_table) in enumerate(spw_and_cal_tables):
        logger.info(
            f"Apply solutions {idx + 1} of {len(spw_and_cal_tables)}, {spw_str}"
        )
        applycal(
            container=casa_container,
            bind_dirs=(cal_ms.path.parent, cal_table.parent),
            vis=str(cal_ms.path),
            gaintable=str(cal_table),
            spw=spw_str,
            flagbackup=False,
        )

        if archive_cal_table:
            zip_folder(in_path=cal_table)

    flag_versions_table = cal_ms.path.with_suffix(".ms.flagversions")
    if flag_versions_table.exists():
        zip_folder(in_path=flag_versions_table)

    return cal_ms.with_options(column="CORRECTED_DATA")


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    sub_parsers = parser.add_subparsers(dest="mode")

    gaincal_parser = sub_parsers.add_parser("gaincal")
    gaincal_parser.add_argument(
        "ms", type=Path, help="Path to the measurement set to calibrate. "
    )
    gaincal_parser.add_argument(
        "--casa-container",
        type=Path,
        default=None,
        help="Path to the CASA6 singularity container",
    )
    gaincal_parser.add_argument(
        "--round", type=int, default=1, help="Self-calibration round number. "
    )
    gaincal_parser.add_argument(
        "--column", type=str, default="DATA", help="The column to self-calibrate"
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "gaincal":
        gaincal_applycal_ms(
            ms=MS(path=args.ms, column=args.column),
            round=args.round,
            casa_container=args.casa_container,
        )


if __name__ == "__main__":
    cli()
