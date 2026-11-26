"""Checks on the measurement sets whose images are co-added"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from astropy.coordinates import SkyCoord

from flint.exceptions import MSError


def _same_angle(angle: float | None, other: float | None, tolerance_deg: float) -> bool:
    """Whether two angles in radians agree, or are both missing"""
    if angle is None or other is None:
        return angle is other
    difference = np.angle(np.exp(1j * (angle - other)))
    return bool(abs(np.rad2deg(difference)) <= tolerance_deg)


def _same_position(
    position: SkyCoord | None, other: SkyCoord | None, tolerance_arcsec: float
) -> bool:
    """Whether two positions agree, or are both missing"""
    if position is None or other is None:
        return position is other
    return bool(position.separation(other).arcsec <= tolerance_arcsec)


def check_shared_pointing(
    names: Sequence[str],
    pol_axes: Sequence[float | None],
    boresights: Sequence[SkyCoord | None],
    pol_axis_tolerance_deg: float = 0.1,
    boresight_tolerance_arcsec: float = 60.0,
) -> None:
    """Raise ``MSError`` unless every measurement set has the third-axis angle and
    the boresight of the first.

    A mosaic takes both from one measurement set and uses them for all. One that
    differs would have its holography, and its Q and U, rotated wrongly.

    Args:
        names (Sequence[str]): What to call each measurement set in the error
        pol_axes (Sequence[float | None]): The third-axis angle of each, in radians
        boresights (Sequence[SkyCoord | None]): Where the dish pointed for each
        pol_axis_tolerance_deg (float, optional): How far the angles may differ, in degrees. Defaults to 0.1.
        boresight_tolerance_arcsec (float, optional): How far the boresights may differ, in arcsec. Defaults to 60.0.

    Raises:
        MSError: If a third-axis angle or a boresight differs from the first, or only one of the two is recorded
    """
    for name, pol_axis, boresight in zip(names, pol_axes, boresights, strict=True):
        if not _same_angle(pol_axis, pol_axes[0], pol_axis_tolerance_deg):
            msg = (
                f"{name} has a third-axis angle of {pol_axis} rad, but {names[0]} "
                f"has {pol_axes[0]} rad. They can not share a mosaic"
            )
            raise MSError(msg)
        if not _same_position(boresight, boresights[0], boresight_tolerance_arcsec):
            msg = (
                f"{name} has a boresight of {boresight}, but {names[0]} has "
                f"{boresights[0]}. They can not share a mosaic"
            )
            raise MSError(msg)
