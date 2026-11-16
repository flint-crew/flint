"""Self-calibration with DP3, solving against the model column and applying the solutions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import numpy as np

from flint.dp3 import (
    DP3ApplyCalOptions,
    DP3DDECalOptions,
    DP3GainCalOptions,
    dp3_applycal,
    dp3_ddecal,
    dp3_gaincal,
)
from flint.logging import logger
from flint.options import MS, BaseOptions
from flint.selfcal.casa import copy_and_clean_ms_casagain
from flint.utils import zip_folder

if TYPE_CHECKING:
    import h5py

DP3SolveMode: TypeAlias = Literal["dp3ddecal", "dp3gaincal"]


class DP3SelfCalOptions(BaseOptions):
    """Flint's own settings for a round of DP3 self-calibration"""

    normalise_amplitudes: bool = True
    """Scale amplitude solutions to a mean of one, so self-calibration keeps the flux scale"""


def get_soltabs(h5parm: Path) -> list[str]:
    """The names of the solution tables in an h5parm, or none if it is missing or empty."""
    import h5py

    if not h5parm.exists():
        return []
    try:
        with h5py.File(h5parm, "r") as solutions:
            return sorted(
                name
                for solset in solutions.values()
                for name, soltab in solset.items()
                if isinstance(soltab, h5py.Group) and "val" in soltab
            )
    except OSError:
        return []


def get_solve_type(options: DP3DDECalOptions | DP3GainCalOptions) -> str:
    """Find what a round solves for, refusing it if self-calibration cannot apply the solutions.

    This is ``mode`` for DDECal and ``caltype`` for GainCal. Only the types listed
    here can be applied by DP3 applycal; any other, such as ``leakage``, is refused.

    Args:
        options (DP3DDECalOptions | DP3GainCalOptions): The solver options of the round

    Raises:
        ValueError: Raised if self-calibration cannot apply what the round solves for

    Returns:
        str: What the round solves for, e.g. ``scalarphase``
    """
    solve_type = (
        options.mode if isinstance(options, DP3DDECalOptions) else options.caltype
    )
    applicable = (
        "scalar",
        "scalarphase",
        "scalaramplitude",
        "diagonal",
        "diagonalphase",
        "diagonalamplitude",
        "fulljones",
        "tec",
        "tec+phase",
        "rotation",
        "rotation+diagonal",
    )
    if solve_type not in applicable:
        msg = f"Self-calibration cannot apply {solve_type} solutions. Solve for one of {applicable}"

        raise ValueError(msg)

    return solve_type


def get_corrections(h5parm: Path, solve_type: str) -> DP3ApplyCalOptions:
    """The applycal corrections for a round's solutions, in the order that undoes them."""
    soltabs = get_soltabs(h5parm)
    # DP3 can only apply full-Jones solutions as one correction
    if solve_type == "fulljones":
        return DP3ApplyCalOptions(correction=["fulljones"], soltab=soltabs)
    # DP3 solves rotation+diagonal as the diagonal times the rotation, so the
    # diagonal has to be undone first
    return DP3ApplyCalOptions(
        correction=sorted(soltabs, key=lambda name: name.startswith("rotation"))
    )


def get_cross_hand_mask(soltab: h5py.Group) -> np.ndarray:
    """Which of a soltab's solutions are cross-hands (e.g. XY), shaped to broadcast against them."""
    axes = soltab["val"].attrs["AXES"]
    axes = (axes.decode() if isinstance(axes, bytes) else axes).split(",")
    is_cross = np.array([pol[0] != pol[1] for pol in soltab["pol"].asstr()[:]])
    shape = [1] * soltab["val"].ndim
    shape[axes.index("pol")] = is_cross.size

    return is_cross.reshape(shape)


def normalise_amplitudes(h5parm: Path) -> Path:
    """Scale each amplitude table in an h5parm, in place, so its good solutions average one.

    This keeps self-calibration from changing the flux scale, like CASA's ``solnorm``,
    though that divides by the root mean square rather than the mean. Full-Jones
    cross-hands (e.g. XY) are near zero, so are left out of the mean.

    Args:
        h5parm (Path): The solutions to normalise

    Returns:
        Path: The same h5parm
    """
    import h5py

    with h5py.File(h5parm, "r+") as solutions:
        for solset in solutions.values():
            for name, soltab in solset.items():
                if not name.startswith("amplitude"):
                    continue
                values = soltab["val"][:]
                good = np.isfinite(values) & (soltab["weight"][:] > 0)
                if "pol" in soltab:
                    good &= ~get_cross_hand_mask(soltab)
                if not good.any():
                    logger.warning(f"No good amplitudes in {h5parm}:{name}")
                    continue
                mean = values[good].mean()
                logger.info(f"Normalising {h5parm}:{name} by {mean=}")
                soltab["val"][:] = values / mean

    return h5parm


def dp3_selfcal_ms(
    ms: MS,
    selfcal_round: int = 1,
    mode: DP3SolveMode = "dp3ddecal",
    update_dp3_options: dict[str, Any] | None = None,
    update_dp3_selfcal_options: dict[str, Any] | None = None,
    archive_input_ms: bool = False,
    skip_selfcal: bool = False,
    rename_ms: bool = False,
) -> MS:
    """Self-calibrate a measurement set with DP3 into a new ``roundN`` measurement set.

    The latest corrected data is solved against the model column, and the solutions
    are written to an h5parm beside the new measurement set. Should that h5parm
    already exist, from an earlier attempt, it is applied without solving again.

    Args:
        ms (MS): The measurement set, with the latest corrected data as its column and its model column set
        selfcal_round (int, optional): The self-calibration round, used to name the outputs. Defaults to 1.
        mode (DP3SolveMode, optional): Solve with ``ddecal`` or ``gaincal``. Defaults to "dp3ddecal".
        update_dp3_options (dict[str, Any] | None, optional): Overrides for the solver's default options. Defaults to None.
        update_dp3_selfcal_options (dict[str, Any] | None, optional): Overrides for the default ``DP3SelfCalOptions``. Defaults to None.
        archive_input_ms (bool, optional): Zip the input measurement set once copied. Ignored if ``rename_ms``. Defaults to False.
        skip_selfcal (bool, optional): Make the new measurement set without calibrating it. Defaults to False.
        rename_ms (bool, optional): Rename the input measurement set rather than copy it. Defaults to False.

    Raises:
        ValueError: Raised if self-calibration cannot apply what the round solves for

    Returns:
        MS: The new measurement set, with ``CORRECTED_DATA`` as its column
    """
    options_class = DP3DDECalOptions if mode == "dp3ddecal" else DP3GainCalOptions
    options = options_class().with_options(**(update_dp3_options or {}))
    solve_type = get_solve_type(options)
    selfcal_options = DP3SelfCalOptions().with_options(
        **(update_dp3_selfcal_options or {})
    )
    logger.info(
        f"Self-calibrating {ms.path} with {mode}: {options=} {selfcal_options=}"
    )

    cal_ms = copy_and_clean_ms_casagain(
        ms=ms, selfcal_round=selfcal_round, rename_ms=rename_ms
    )
    if archive_input_ms and not rename_ms:
        zip_folder(in_path=ms.path)

    if skip_selfcal:
        logger.info(f"{skip_selfcal=}, not calibrating the MS. ")
        return cal_ms

    h5parm = cal_ms.path.with_suffix(".h5")
    if get_soltabs(h5parm):
        logger.warning(f"Found an earlier {h5parm=}. Not solving again!")
    else:
        h5parm.unlink(missing_ok=True)
        solve = dp3_ddecal if mode == "dp3ddecal" else dp3_gaincal
        solve(cal_ms, options, h5parm=h5parm)
    # Normalising twice changes nothing, so a restart can safely do it again
    if selfcal_options.normalise_amplitudes:
        normalise_amplitudes(h5parm)

    return dp3_applycal(
        cal_ms, h5parm=h5parm, options=get_corrections(h5parm, solve_type)
    )
