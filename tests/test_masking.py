from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from eye_patch.naming import FITSMaskNames

from flint.imager.wsclean import ImageSet
from flint.masking import consider_beam_mask_round
from flint.options import MS
from flint.prefect.common.imaging import (
    task_create_image_mask_model,
    task_flatten_mask,
    task_wsclean_imager,
)


def test_consider_beam_masking_round():
    """Test to ensure the beam mask consideration log is correct"""
    lower = ("all", "ALL", "aLl")
    states = (
        consider_beam_mask_round(current_round=1, mask_rounds=low) for low in lower
    )

    assert all(states)

    assert consider_beam_mask_round(current_round=3, mask_rounds=1)
    assert not consider_beam_mask_round(current_round=0, mask_rounds=1)

    assert consider_beam_mask_round(current_round=3, mask_rounds=(1, 2, 3, 4, 5))
    assert not consider_beam_mask_round(current_round=3, mask_rounds=(1, 2, 4, 5))

    assert not consider_beam_mask_round(current_round=3, mask_rounds=None)

    assert not consider_beam_mask_round(
        current_round=3, mask_rounds=1, allow_beam_masks=False
    )
    assert consider_beam_mask_round(
        current_round=3, mask_rounds=1, allow_beam_masks=True
    )
    assert consider_beam_mask_round(current_round=1, mask_rounds=1)


@pytest.mark.parametrize(
    "multiscale, mask_field, expected_option",
    [
        (True, "scale_mask_fits", "multiscale_fits_mask"),
        (False, "scale_mask_fits", "fits_mask"),
        (True, "mask_fits", "fits_mask"),
        (False, "mask_fits", "fits_mask"),
    ],
)
def test_task_wsclean_imager_mask_routing(
    monkeypatch, multiscale, mask_field, expected_option
):
    """A per-scale mask is only passed to wsclean as one in a multiscale round"""
    wsclean_options: dict[str, object] = {}
    monkeypatch.setattr(
        "flint.prefect.common.imaging.wsclean_imager",
        lambda **kwargs: wsclean_options.update(kwargs["update_wsclean_options"]),
    )
    mask_path = Path("image.mask.fits")

    task_wsclean_imager.fn(
        in_ms=MS(path=Path("example.ms")),
        wsclean_container=Path("wsclean.sif"),
        update_wsclean_options={"multiscale": multiscale},
        fits_mask=FITSMaskNames(**{mask_field: mask_path}),
    )

    masks = {key: value for key, value in wsclean_options.items() if "mask" in key}
    assert masks == {expected_option: mask_path}


def test_task_create_image_mask_model_scales(tmp_path):
    """Eroding at several scales makes a per-scale mask"""
    rng = np.random.default_rng(42)
    image = rng.normal(0, 1, (1, 1, 256, 256)).astype(np.float32)
    yy, xx = np.mgrid[:256, :256]
    image[0, 0] += 50 * np.exp(-((xx - 128) ** 2 + (yy - 128) ** 2) / (2 * 6**2))
    header = fits.Header(
        dict(
            CDELT1=-0.000694444444444444,
            CDELT2=0.000694444444444444,
            BMAJ=0.0034,
            BMIN=0.0028,
            BPA=74.6,
        )
    )
    image_path = tmp_path / "image.fits"
    fits.writeto(image_path, image, header)

    names = task_create_image_mask_model.fn(
        image=ImageSet(prefix="image", image=[image_path]),
        update_masking_options={
            "use_mac": True,
            "flood_fill": True,
            "beam_shape_erode": True,
            "multiscale_scales": (0, 4),
        },
    )

    assert names.mask_fits is None
    assert names.scale_mask_fits is not None and names.scale_mask_fits.exists()
    assert np.max(fits.getdata(names.scale_mask_fits)) == 3


def test_task_flatten_mask(tmp_path):
    """A per-scale mask becomes a plain mask set wherever any scale is"""
    scale_mask = np.array([[0, 1, 2], [3, 0, 4]], dtype=np.float32)
    scale_mask_path = tmp_path / "image.scalemask.fits"
    fits.writeto(scale_mask_path, scale_mask)

    names = task_flatten_mask.fn(
        fits_mask=FITSMaskNames(scale_mask_fits=scale_mask_path)
    )

    assert names.scale_mask_fits is None
    assert names.mask_fits == tmp_path / "image.mask.fits"
    assert np.array_equal(fits.getdata(names.mask_fits), scale_mask != 0)

    # A rerun replaces the mask its first attempt wrote
    task_flatten_mask.fn(fits_mask=FITSMaskNames(scale_mask_fits=scale_mask_path))


def test_task_flatten_mask_leaves_a_plain_mask():
    """A mask that is not per-scale is returned as is"""
    names = FITSMaskNames(mask_fits=Path("image.mask.fits"))

    assert task_flatten_mask.fn(fits_mask=names) is names
