"""Utility functions to carry out flagging against ASKAP measurement sets"""

from __future__ import annotations

import math
from argparse import ArgumentParser
from pathlib import Path
from typing import Collection, NamedTuple

import numpy as np
from casacore.tables import table
from numpy.typing import NDArray

from flint.exceptions import MSError
from flint.logging import logger
from flint.ms import check_column_in_ms, critical_ms_interaction, describe_ms
from flint.options import MS
from flint.sclient import run_singularity_command
from flint.utils import get_packaged_resource_path


class AOFlaggerCommand(NamedTuple):
    """The command to use when running aoflagger"""

    cmd: str
    """The command that will be executed"""
    ms_path: Path
    """The path to the MS that will be flagged. """
    ms: MS
    """The MS object that was flagged"""
    strategy_file: Path | None = None
    """The path to the aoflagging strategy file to use"""


def flag_ms_zero_uvws(ms: MS, chunk_size: int = 10000) -> MS:
    """Flag out the UVWs in a measurement set that have values of zero.
    This happens when some data are flagged before it reaches the TOS.

    A critical MS interaction scope is created to ensure if things fail
    they are known.

    Args:
        ms (MS): Measurement set to flag
        chunk_size (int, optional): The number of rows to flag at a tim. Defaults to 10000.

    Returns:
        MS: The flagged measurement set
    """

    ms = MS.cast(ms)
    logger.info(f"Flagging zero uvw's for {ms.path}")
    row_idx = 0

    # Rename the measurement set while it is being operated on
    with critical_ms_interaction(input_ms=ms.path) as critical_ms_path:
        with table(str(critical_ms_path), readonly=False, ack=False) as tab:
            table_size = len(tab)

            # so long as the row index is less than the table size there
            # is another chunk to flag
            while row_idx < (table_size - 1):
                uvws = tab.getcol("UVW", startrow=row_idx, nrow=chunk_size)
                flags = tab.getcol("FLAG", startrow=row_idx, nrow=chunk_size)

                # Select records what the (u,v,w) are (0,0,0)
                # Data in the shape (record, 3)
                zero_uvws = np.all(uvws == 0, axis=1)
                flags[zero_uvws, :] = True

                # Put it back into place, update the counter for the next insertion
                size = len(flags)
                tab.putcol("FLAG", flags, startrow=row_idx, nrow=size)
                row_idx += size

                # Ensure changes written back to the MS
                tab.flush()

    return ms


def _nan_zero_extreme_flag(
    data: NDArray[np.complex128],
    flags: NDArray[np.bool],
    uvws: NDArray[np.float64],
    flag_extreme_dxy: bool = True,
    dxy_thresh: float = 4.0,
) -> tuple[NDArray[np.complex128], NDArray[np.bool], NDArray[np.float64]]:
    """Used to evaluate the flags that should be applied to the MS based on
    data values (if there are 0's or nans), uvw values (which can be 0's) and
    extreme stokes-v values.

    This is the internal checker for the larger `nan_zero_extreme_flags_in_ms`
    function.

    Args:
        data (NDArray[np.complex128]): Data that is being consider drawn from a `DATA` like column
        flags (NDArray[np.bool]): The flags that correspond to the `DATA` column drawn from the `FLAG` column
        uvws (NDArray[np.float64]): The (U,V,W) coordinates of the data
        flag_extreme_dxy (bool, optional): Whether flagging on extreme stokes-v should be consider. Defaults to True.
        dxy_thresh (float, optional): The threshold of the stokes-v flagging. Defaults to 4.0.

    Returns:
        tuple[NDArray[np.complex128], NDArray[np.bool], NDArray[np.float64]]: Copies of the input `data`, `flags` and `uvws`. The `flags` are updated based on the results of the flagging.
    """

    no_flags_before = np.sum(flags)

    nan_mask = np.where(~np.isfinite(data))
    zero_mask = np.where(data == 0 + 0j)
    uvw_mask = np.any(uvws == 0, axis=1)
    logger.info(
        f"Will flag {np.sum(nan_mask)} NaNs, zero'd data {np.sum(zero_mask)}, zero'd UVW {np.sum(uvw_mask)}. "
    )

    flags[nan_mask] = True
    flags[zero_mask] = True
    flags[uvw_mask] = True

    if flag_extreme_dxy:
        logger.info(f"Flagging based on extreme Stokes-V, threshold {dxy_thresh=}")
        dxy = np.abs(data[:, :, 1] - data[:, :, 2])
        dxy_mask = np.where(dxy > dxy_thresh)
        logger.info(f"Will flag {np.sum(dxy_mask)} extreme Stokes-V.")

        # TODO: This can be compressed to a one-liner
        flags[:, :, 0][dxy_mask] = True
        flags[:, :, 1][dxy_mask] = True
        flags[:, :, 2][dxy_mask] = True
        flags[:, :, 3][dxy_mask] = True

    no_flags_after = np.sum(flags)
    logger.info(
        f"Flags before: {no_flags_before}, Flags after: {no_flags_after}, Difference {no_flags_after - no_flags_before}"
    )

    return (data, flags, uvws)


def nan_zero_extreme_flag_ms(
    ms: Path | MS,
    data_column: str | None = None,
    flag_extreme_dxy: bool = True,
    dxy_thresh: float = 4.0,
    nan_data_on_flag: bool = False,
    chunk_size: int = 100000,
) -> MS:
    """Will flag a MS based on NaNs or zeros in the nominated data column of a measurement set.
    These NaNs might be introduced into a column via the application of a applysolutions task.
    Zeros might be introduced by the correlator dropping cycles and not appropriately setting the
    corresponding FLAG column (although this might have been fixed).

    There is also an optional component to flag based on extreme Stokes-V values.

    Visibilities that are marked as bad will have the FLAG column updatede appropriately.

    Args:
        ms (Union[Path,MS]): The measurement set that will be processed and have visibilities flagged.
        data_column (Optional[str], optional): The column to inspect. This will override the value in the nominated column of the MS. Defaults to None.
        flag_extreme_dxy (bool, optional): Whether Stokes-V will be inspected and flagged. Defaults to True.
        dxy_thresh (float, optional): Threshold used in the Stokes-V case. Defaults to 4.0.
        nan_data_on_flag (bool, optional): If True, data whose FLAG is set to True will become NaNs. Defaults to False.
        chunk_size (int, optional): Number of rows to process at a time. Defaults to 100000.

    Returns:
        MS: The container of the processed MS
    """
    ms = MS.cast(ms)

    if data_column is None and ms.column is None:
        logger.warning("No valid data column selected, using default of DATA")
        data_column = "DATA"
    elif data_column is None and ms.column is not None:
        logger.info(f"Using nominated {ms.column} column for {ms.path!s}")
        data_column = ms.column

    logger.info(f"Flagging NaNs and zeros in {data_column}.")

    with table(str(ms.path), readonly=False, ack=False) as tab:
        no_rows = len(tab)
        no_chunks = math.ceil(no_rows / chunk_size)
        logger.info(f"Number of rows: {no_rows} across {no_chunks}")
        idx = 0
        while idx * chunk_size < no_rows:
            start_row = idx * chunk_size
            logger.info(
                f"Flagging {idx + 1} of {no_chunks} - {start_row=} with {chunk_size=}"
            )

            data = tab.getcol(data_column, startrow=start_row, nrow=chunk_size)
            flags = tab.getcol("FLAG", startrow=start_row, nrow=chunk_size)
            uvws = tab.getcol("UVW", startrow=start_row, nrow=chunk_size)

            data, flags, uvws = _nan_zero_extreme_flag(
                data=data,
                flags=flags,
                uvws=uvws,
                flag_extreme_dxy=flag_extreme_dxy,
                dxy_thresh=dxy_thresh,
            )

            logger.info("Adding updated flags column")
            tab.putcol("FLAG", flags, startrow=start_row, nrow=chunk_size)

            # Keep this outside the above function to avoid
            # passing the table reference. To avoid thrashing the data
            # column wwe would otherwise check to see if this option is enabled
            # when putting the column. May as well keep it here.
            if nan_data_on_flag:
                data[flags] = np.nan
                logger.info(f"Setting {np.sum(flags)} DATA items to NaN.")
                tab.putcol(data_column, data, startrow=start_row, nrow=chunk_size)
            tab.flush()
            idx += 1

        tab.close()

    return ms


def create_aoflagger_cmd(ms: MS) -> AOFlaggerCommand:
    """Create a command to run aoflagger against a measurement set

    Args:
        ms (MS): The measurement set to flag. The column attached to the MS.column
        is flagged.

    Raises:
        MSError: Raised when the attached column is not found in the MS

    Returns:
        AOFlaggerCommand: The aoflagger command that will be run
    """
    logger.info("Creating an AOFlagger command. ")
    ms = MS.cast(ms)

    assert ms.column is not None, (
        f"MS column must be set in order to flag, currently {ms.column=}. Full {ms=}"
    )

    if not check_column_in_ms(ms):
        raise MSError(f"Column {ms.column} not found in {ms.path}.")

    flagging_strategy = get_packaged_resource_path(
        package="flint.data.aoflagger", filename="ASKAP.lua"
    )
    logger.info(f"Flagging using the strategy file {flagging_strategy}")

    cmd = f"aoflagger -column {ms.column} -strategy {flagging_strategy} -v {ms.path.absolute()!s}"

    return AOFlaggerCommand(
        cmd=cmd, ms_path=ms.path, strategy_file=Path(flagging_strategy), ms=ms
    )


def run_aoflagger_cmd(aoflagger_cmd: AOFlaggerCommand, container: Path) -> None:
    """Run the aoflagger command constructed in its singularity container

    Args:
        aoflagger_cmd (AOFlaggerCommand): The command that will be executed
        container (Path): Path to the container that contains aoflagger
    """
    assert container.exists(), (
        f"The applysolutions container {container} does not exist. "
    )

    bind_dirs = [aoflagger_cmd.ms_path.parent.absolute()]
    logger.debug(f"Bind directory for aoflagger: {bind_dirs}")

    if aoflagger_cmd.strategy_file:
        bind_dirs.append(aoflagger_cmd.strategy_file)

    run_singularity_command(
        image=container.absolute(), command=aoflagger_cmd.cmd, bind_dirs=bind_dirs
    )


def flag_ms_aoflagger(ms: MS, container: Path) -> MS:
    """Create and run an aoflagger command in a container

    Args:
        ms (MS): The measurement set with nominated column to flag
        container (Path): The container with the aoflagger program

    Returns:
        MS: Measurement set flagged with the appropriate column
    """
    ms = MS.cast(ms)
    logger.info(f"Will flag column {ms.column} in {ms.path!s}.")
    aoflagger_cmd = create_aoflagger_cmd(ms=ms)

    logger.info("Flagging command constructed. ")
    run_aoflagger_cmd(aoflagger_cmd=aoflagger_cmd, container=container)

    # TODO: This should be moved to the aoflagger lua file once it has
    # been implemented
    ms = flag_ms_zero_uvws(ms=ms)

    return ms


def flag_ms_by_antenna_ids(ms: Path | MS, ant_ids: int | Collection[int]) -> MS:
    """Set the FLAG to True for a collection of rows where ANTENNA1 or ANTENNA2 is in a set of
    antenna IDs to flag. The flagging is performed via the antenna ID as it is in the measurement
    set - it is not by the antenna name.

    Args:
        ms (Union[Path, MS]): The measurement set that has antennas to flag
        ant_ids (Union[int,Collection[int]]): The set of antenna IDs to flag.

    Returns:
        MS: The measurement set with flagged antennas.
    """
    ms = MS.cast(ms)

    ant_ids = (ant_ids,) if isinstance(ant_ids, int) else ant_ids

    if len(ant_ids) == 0:
        logger.info("Antenna list to flag is empty. Exiting. ")
        return ms

    logger.info(f"Will flag {ms.path!s}.")
    logger.info(f"Antennas to flag: {ant_ids}")

    # TODO: Potentially this should be batched into chunks to operate over
    with table(str(ms.path), readonly=False, ack=False) as tab:
        logger.info(f"Opened {ms.path!s}, loading metadata.")
        ant1 = tab.getcol("ANTENNA1")
        ant2 = tab.getcol("ANTENNA2")
        flags = tab.getcol("FLAG")

        init_flags = np.sum(flags)

        for ant_id in ant_ids:
            ant_mask = (ant_id == ant1) | (ant_id == ant2)

            if not np.any(ant_mask):
                logger.info(f"No data for {ant_id=} found. Continuing. ")
                continue

            logger.info(f"Flagging {ant_id=}.")
            flags[ant_mask] = True

        tab.putcol("FLAG", flags)
        end_flags = np.sum(flags)

    diff_flags = end_flags - init_flags
    logger.info(
        f"Loaded flags: {init_flags}, Final flags: {end_flags}, Difference: {diff_flags} ({diff_flags / np.prod(flags.shape) * 100.0:.2f}%)"
    )

    return ms


def get_parser() -> ArgumentParser:
    """Create the argument parser for the flagging

    Returns:
        ArgumentParser: aoflagger argument parser
    """
    parser = ArgumentParser(description="Run aoflagger against a measurement set")

    subparser = parser.add_subparsers(dest="mode")

    aoflagger_parser = subparser.add_parser(
        "aoflagger", description="Run AOFlagger against an measurement set. "
    )
    aoflagger_parser.add_argument("ms", type=Path, help="The measurement set to flag")
    aoflagger_parser.add_argument(
        "--aoflagger-container",
        type=Path,
        default=Path("aoflagger.sif"),
        help="The container that holds the aoflagger application",
    )
    aoflagger_parser.add_argument(
        "--column", type=str, default="DATA", help="The column of data in MS to flag"
    )

    nan_zero_parser = subparser.add_parser(
        "nanflag", help="Flag visibilities that are either NaN or zeros. "
    )
    nan_zero_parser.add_argument(
        "ms", type=Path, help="The measurement set that will be flagged. "
    )
    nan_zero_parser.add_argument(
        "--column", type=str, default="DATA", help="The column of data in MS to flag"
    )
    nan_zero_parser.add_argument(
        "--flag-extreme-dxy",
        action="store_true",
        help="Flag visibilities whose ABS(XY - YX) change significantly",
    )
    nan_zero_parser.add_argument(
        "--dxy-thresh",
        type=float,
        default=4.0,
        help="If extreme dxy flagging, ABS(XY - YX) above this value will be flagged. ",
    )
    nan_zero_parser.add_argument(
        "--nan-data-on-flag",
        action="store_true",
        help="NaN the data if their FLAG attribute is True. ",
    )

    antenna_parser = subparser.add_parser("antenna", help="Flag data by the antenna ID")
    antenna_parser.add_argument(
        "ms", type=Path, help="Path to the measurement set that will be flagged. "
    )
    antenna_parser.add_argument(
        "antenna_ids",
        type=int,
        nargs="+",
        help="The antenna IDs of the rows that should be flagged. ",
    )
    return parser


def cli() -> None:
    import logging

    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "aoflagger":
        ms = MS(path=args.ms, column=args.column)

        describe_ms(ms)
        flag_ms_aoflagger(ms=ms, container=args.aoflagger_container)
        describe_ms(ms)
    elif args.mode == "nanflag":
        nan_zero_extreme_flag_ms(
            ms=args.ms,
            data_column=args.column,
            flag_extreme_dxy=args.flag_extreme_dxy,
            dxy_thresh=args.dxy_thresh,
            nan_data_on_flag=args.nan_data_on_flag,
        )
    elif args.mode == "antenna":
        flag_ms_by_antenna_ids(ms=args.ms, ant_ids=args.antenna_ids)


if __name__ == "__main__":
    cli()
