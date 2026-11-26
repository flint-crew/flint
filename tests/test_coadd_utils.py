"""Tests for the checks made before images are co-added"""

from __future__ import annotations

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord

from flint.coadd.utils import check_shared_pointing
from flint.exceptions import MSError


def _boresight(offset_arcsec: float = 0.0) -> SkyCoord:
    return SkyCoord(246.5 * u.deg, -84.1 * u.deg + offset_arcsec * u.arcsec)


def test_check_shared_pointing_accepts_measurement_sets_that_agree() -> None:
    """Angles a turn apart are the same angle, and a small pointing difference
    does not matter"""
    check_shared_pointing(
        names=["low", "mid", "high"],
        pol_axes=[1.2, 1.2 + 2 * np.pi, 1.2 + np.deg2rad(0.05)],
        boresights=[_boresight(), _boresight(), _boresight(offset_arcsec=5.0)],
    )
    check_shared_pointing(
        names=["low", "mid"], pol_axes=[None] * 2, boresights=[None] * 2
    )
    check_shared_pointing(names=[], pol_axes=[], boresights=[])


def test_check_shared_pointing_refuses_a_different_third_axis() -> None:
    with pytest.raises(MSError, match="mid has a third-axis angle"):
        check_shared_pointing(
            names=["low", "mid"],
            pol_axes=[1.2, 1.2 + np.deg2rad(1.0)],
            boresights=[_boresight(), _boresight()],
        )


def test_check_shared_pointing_refuses_a_different_boresight() -> None:
    with pytest.raises(MSError, match="mid has a boresight"):
        check_shared_pointing(
            names=["low", "mid"],
            pol_axes=[1.2, 1.2],
            boresights=[_boresight(), _boresight(offset_arcsec=600.0)],
        )


def test_check_shared_pointing_refuses_one_recorded_and_one_not() -> None:
    """One measurement set with a boresight and one without were not prepared
    the same way"""
    with pytest.raises(MSError, match="boresight"):
        check_shared_pointing(
            names=["low", "mid"], pol_axes=[1.2, 1.2], boresights=[_boresight(), None]
        )
    with pytest.raises(MSError, match="third-axis"):
        check_shared_pointing(
            names=["low", "mid"], pol_axes=[1.2, None], boresights=[None, None]
        )
