"""Helper functions and tools to update spectific features of ASKAP CASDA related
data sets"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from casacore.tables import table
from numpy.typing import NDArray

from flint.logging import logger
from flint.ms import MS, get_beam_from_ms


def _find_casda_bandpass_table_type(column_name: Sequence[str]) -> str:
    """Examine the bandpass table to determine if it is a CASDA provided table or not.
    This is done by examining the columns of the table and looking for the expected
    columns that are present in a CASDA provided bandpass table.

    Args:
        column_name (Sequence[str]): The columns of the bandpass table to examine

    Raises:
        ValueError: Raised if the table does not appear to be from CASDA

    Returns:
        str: The name of the column with the validity flags for the bandpass solutions. These could be 'BANDPASS_VALID' or 'BPLEAKAGE_VALID' for CASDA provided tables.

    """
    expected_columns: dict[str, Sequence[str]] = {
        "BANDPASS_VALID": ("TIME", "BANDPASS", "BANDPASS_VALID"),
        "BPLEAKAGE_VALID": ("TIME", "BPLEAKAGE", "BPLEAKAGE_VALID"),
    }

    for col_name, expected_cols in expected_columns.items():
        if all(col in column_name for col in expected_cols):
            return col_name

    msg = f"Unable to determine bandpass flag column given input {column_name=}. Expected one of {list(expected_columns.values())=}"
    raise ValueError(msg)


def get_flagged_antenna_casda_solutions(
    bandpass_table: Path,
) -> dict[int, NDArray[np.bool]]:
    """Examine a CASDA provided bandpass table to attempt to identify antennas
    that are completely flagged from their solutions. Examine the `BANDPASS_VALID`
    column to do this. The shape of the bandpass solutions should be:

    >>> (TIME, BEAM, ANT, GAIN)

    The time dimension is enforced to be length 1 in this function.

    Args:
        bandpass_table (Path): Path to the table to examine

    Raises:
        ValueError: Raised if the table does not appear to be from CASDA

    Returns:
        dict[int, NDArray[np.bool]]: Antenna indices that are completely flagged for each beam.
    """

    beam_ant_idxs: dict[int, NDArray[np.bool]] = {}
    with table(str(bandpass_table), ack=False, readonly=True) as tab:
        columns = tab.colnames()

        flag_col_name = _find_casda_bandpass_table_type(columns)
        flags = ~tab.getcol(flag_col_name)

        # Flag shape will be (TIME, BEAM, ANT, GAIN).
        assert flags.shape[0] == 1, f"More then one TIME interval in {flags.shape=}"
        assert len(flags.shape) == 4, (
            f"Expected dimensionality of 4, but got {flags.shape=}"
        )

        # Shape now (BEAM, ANT, GAIN)
        flags = np.squeeze(flags)
        beam_ant_flagged = np.all(flags, axis=2)

        for beam, ant_flags in enumerate(beam_ant_flagged):
            beam_ant_idxs[beam] = np.squeeze(np.argwhere(ant_flags))

    return beam_ant_idxs


def flag_antenna_from_casda_bandpass_table(ms: MS | Path, bandpass_table: Path) -> MS:
    """Flag antennas in a MS if the bandpass table indicates that all solutions are invalid.
    There are instances where resetting the flags may be preferred, and corner cases where
    uncalibrated data may make it into a 'CORRECTED_DATA' column with appropriate flags being
    set.

    Args:
        ms (MS | Path): The measurement set to examine and flag
        bandpass_table (Path): The bandpass table that will be examined to assess flagging

    Returns:
        MS: The measurement set that was modified to update flags
    """

    assert bandpass_table.exists(), f"{bandpass_table=} does not exist"
    beam_to_ant_idxs = get_flagged_antenna_casda_solutions(
        bandpass_table=bandpass_table
    )

    beam = get_beam_from_ms(ms=ms)

    # Nothing to do
    if beam not in beam_to_ant_idxs or len(beam_to_ant_idxs[beam]) == 0:
        logger.info(f"No antennas to flag for {beam=}")
        return MS.cast(ms)

    ant_idx_to_flag = beam_to_ant_idxs[beam]

    _ms: MS = MS.cast(ms)
    assert isinstance(_ms, MS), f"Expected MS type, have {type(ms)}"

    logger.info(f"Flagging {_ms.path=}")
    logger.info(f"{beam=} to have {ant_idx_to_flag=}")
    with table(str(_ms.path), readonly=False, ack=False) as tab:
        for ant_idx in ant_idx_to_flag:
            ant_tab = tab.query(f"ANTENNA1=={ant_idx} OR ANTENNA2=={ant_idx}")
            flags = ant_tab.getcol("FLAG")
            flags_before = np.sum(flags)

            flags = np.ones_like(flags, dtype=bool)
            flags_after = np.sum(flags)

            logger.info(
                f"Flagging {ant_idx=} - flags {flags.shape}, Difference {flags_after - flags_before=}, {flags_before=} vs {flags_after=}"
            )
            ant_tab.putcol(columnname="FLAG", value=flags)
            ant_tab.flush()

    return _ms
