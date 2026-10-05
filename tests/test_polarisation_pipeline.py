"""Tests around the polarisation pipeline helpers"""

from __future__ import annotations

from flint.configuration import Strategy
from flint.prefect.flows.polarisation_pipeline import _polarisations_to_image


def test_polarisations_to_image_defaults_to_total():
    """No polarisation operation at all means Stokes I"""
    assert _polarisations_to_image(strategy=Strategy()) == {"total": {}}


def test_polarisations_to_image_keeps_polarisations():
    """Polarisation keys and their scoped modes are carried through"""
    strategy = Strategy(
        polarisation={
            "total": {"wsclean": {"niter": 10}},
            "linear": {"wsclean": {"niter": 20}},
        }
    )
    assert _polarisations_to_image(strategy=strategy) == {
        "total": {"wsclean": {"niter": 10}},
        "linear": {"wsclean": {"niter": 20}},
    }


def test_polarisations_to_image_drops_operation_modes():
    """Modes applied across all polarisations are not polarisations to image"""
    strategy = Strategy(
        polarisation={
            "fftbane": {"step_size": 16},
            "fitscube": {"compress": True},
            "linear": {"wsclean": {"niter": 20}},
        }
    )
    assert _polarisations_to_image(strategy=strategy) == {
        "linear": {"wsclean": {"niter": 20}}
    }


def test_polarisations_to_image_modes_only_defaults_to_total():
    """A polarisation operation carrying only modes still images Stokes I"""
    strategy = Strategy(polarisation={"fftbane": {"step_size": 16}})
    assert _polarisations_to_image(strategy=strategy) == {"total": {}}
