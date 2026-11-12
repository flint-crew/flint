"""Tests around the polarisation pipeline helpers"""

from __future__ import annotations

from pathlib import Path

from eye_patch.naming import FITSMaskNames

from flint.configuration import POLARISATION_MAPPING, Strategy
from flint.prefect.flows.polarisation_pipeline import (
    _polarisations_to_image,
    get_polarisation_fits_masks,
)


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


def test_get_polarisation_fits_masks():
    """Stokes I gets each beam's mask as is, and every other polarisation the
    flattened one"""
    masks = [
        FITSMaskNames(scale_mask_fits=Path(f"beam{i}.scalemask.fits")) for i in (0, 1)
    ]
    flat_masks = [FITSMaskNames(mask_fits=Path(f"beam{i}.mask.fits")) for i in (0, 1)]

    for polarisation in POLARISATION_MAPPING:
        expected = masks if polarisation == "total" else flat_masks
        assert (
            get_polarisation_fits_masks(
                polarisation=polarisation,
                fits_masks_by_beam=masks,
                flat_fits_masks=flat_masks,
                n_beams=2,
            )
            == expected
        )


def test_get_polarisation_fits_masks_without_masks():
    """Without masks, or with a beam whose mask failed, a beam is imaged without one"""
    for polarisation in POLARISATION_MAPPING:
        assert get_polarisation_fits_masks(
            polarisation=polarisation,
            fits_masks_by_beam=None,
            flat_fits_masks=None,
            n_beams=3,
        ) == [None, None, None]

    mask = FITSMaskNames(mask_fits=Path("beam0.mask.fits"))
    assert get_polarisation_fits_masks(
        polarisation="linear",
        fits_masks_by_beam=[mask, None],
        flat_fits_masks=[mask, None],
        n_beams=2,
    ) == [mask, None]


def test_get_polarisation_fits_masks_refuses_a_plain_and_per_scale_mask():
    """A mask with both a plain and a per-scale mask set is refused"""
    import pytest

    both = FITSMaskNames(
        mask_fits=Path("beam0.mask.fits"),
        scale_mask_fits=Path("beam0.scalemask.fits"),
    )

    with pytest.raises(AssertionError, match="both plain and per-scale"):
        get_polarisation_fits_masks(
            polarisation="total",
            fits_masks_by_beam=[both],
            flat_fits_masks=[None],
            n_beams=1,
        )
