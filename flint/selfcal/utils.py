"""Gemeral help functions that could be used for self-calibration
across different packages.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path

from flint.exceptions import MSError
from flint.flagging import nan_zero_extreme_flag_ms
from flint.logging import logger
from flint.ms import get_freqs_from_ms, rename_ms_and_columns_for_selfcal
from flint.naming import get_selfcal_ms_name
from flint.options import MS
from flint.utils import remove_files_folders


def create_and_check_caltable_path(
    ms: MS, channel_range: tuple[int, int] | None = None, remove_if_exists: bool = False
) -> Path:
    """Create the output name path for the gaincal solutions table.

    If the table already exists it will be removed.

    Args:
        cal_ms (MS): A description of the measurement set
        channel_range (tuple[int,int] | None, optional): Channel start and end, which will be appended. Defaults to None.
        remove_if_exists (bool, optional): If ``True`` and the table exists, remove it. Defaults to False.

    Returns:
        Path: Output path of the solutions table
    """

    cal_suffix = ".caltable"
    if channel_range:
        cal_suffix += f".ch{channel_range[0]:04d}-{channel_range[1]:04d}"
    cal_table_name = ms.path.with_suffix(cal_suffix)

    cal_table = ms.path.absolute().parent / cal_table_name
    logger.info(f"Will create calibration table {cal_table}.")

    if remove_if_exists and cal_table.exists():
        logger.warning(f"Removing {cal_table!s}")
        remove_files_folders(cal_table)

    return cal_table


def get_channel_ranges_given_nspws(
    num_channels: int, nspws: int
) -> tuple[tuple[int, int], ...]:
    """Given the number of channels construct the start and end channel indices
    for the specified number of spectral windows. The interval step size will be ceiled
    should the the ``num_channels / nspw`` not be whole. In this case the last
    interval will be smaller than the others.

    Args:
        num_channels (int): The number of channels spanning some band
        nspws (int): The number of segments across the band

    Returns:
        tuple[tuple[int,int]]: The start and end channel index spanning the number of channels.
    """

    step = ceil(num_channels / nspws)
    starts = list(range(0, num_channels, step))
    ends = [min(start + step - 1, num_channels - 1) for start in starts]

    return tuple(zip(starts, ends))


def get_channel_ranges_given_nspws_for_ms(
    ms: MS | Path, nspw: int
) -> tuple[tuple[int, int], ...]:
    """Construct channel range intervals for the channels in a measurement set
    given a desired number of spectral windows

    Args:
        ms (MS | Path): The measurement set to construct channel ranges for
        nspw (int): Number of channel intervals to construct

    Returns:
        tuple[tuple[int,int], ...]: The collection of start and end channel intervals. The length will be ``nspw``
    """
    ms = MS.cast(ms=ms)

    logger.info(f"Considering {ms.path}, obtaining channel ranges for {nspw=}")
    freqs = get_freqs_from_ms(ms=ms)

    return get_channel_ranges_given_nspws(num_channels=len(freqs), nspws=nspw)


def consider_skip_selfcal_on_round(
    current_round: int, skip_selfcal_on_rounds: int | list[int] | None
) -> bool:
    """Consider whether the self-calibration process (derive and applying solutions)
    should be skipped on a particular imaging round.

    Should `current_round` be in `skip_selfcal_on_round` then the self-calibration
    should be skipped and a `True` is returned.

    Args:
        current_round (int): The current imaging round being considered
        skip_selfcal_on_rounds (Union[int, List[int], None]): The set of rounds that should be considerede for skipping. If None a False is returned.

    Returns:
        bool: Whether the round is skipped (True) or performed (False)
    """

    # Consider whether this is unset, in which case all rounds should be self-cal
    if skip_selfcal_on_rounds is None:
        return False

    # For sanity consider a single int case
    skip_selfcal_on_rounds = (
        skip_selfcal_on_rounds
        if isinstance(skip_selfcal_on_rounds, list)
        else [skip_selfcal_on_rounds]
    )

    return current_round in skip_selfcal_on_rounds


def prepare_ms_for_selfcal(ms: MS, round: int, keep_old_columns: bool = False) -> MS:
    """Rename a measurement set for a round of self-calibration, move its
    nominated column into DATA, and flag bad visibilities.

    Args:
        ms (MS): The measurement set whose nominated column was last imaged
        round (int): The self-calibration round, used in the new name
        keep_old_columns (bool, optional): Keep the replaced DATA column as ``DATA_ROUND{round - 1}`` rather than removing it. Defaults to False.

    Raises:
        MSError: Raised when ``ms`` has no nominated column

    Returns:
        MS: The renamed measurement set, with DATA as its column
    """
    out_ms_path = get_selfcal_ms_name(in_ms_path=ms.path, round=round)

    if not ms.path.exists() and out_ms_path.exists():
        logger.info(f"Resuming from the already renamed {out_ms_path}")
        ms = ms.with_options(path=out_ms_path, column="DATA")
    else:
        if not ms.column:
            raise MSError(f"No column has been assigned: {ms}")

        ms = rename_ms_and_columns_for_selfcal(
            ms=ms,
            target=out_ms_path,
            corrected_data=ms.column,
            old_data_column=f"DATA_ROUND{round - 1}" if keep_old_columns else None,
        )

    return nan_zero_extreme_flag_ms(ms=ms)
