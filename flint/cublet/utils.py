"""Utilitity functions and options around the cublet imaging"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from numpy.typing import NDArray
from scipy.ndimage import label

from flint.logging import logger
from flint.naming import FITSMaskNames


@dataclass
class CubletRegion:
    """Information around a single cublet region"""

    fits_mask: Path
    """Path to the mask in the first fill regresenting the image region"""
    idx: int
    """Island index number"""
    size: tuple[int, int]
    """Size of the region that the cublet represents"""
    shift_center: str
    """Shift center of the region"""


@dataclass
class FITSMaskData:
    """Temporary container to holder FITS related information"""

    path: Path
    """Path to the loaded file"""
    header: fits.Header
    """Header of the file"""
    norm_data: NDArray[np.bool]
    """Mask data that has been normalised tobools for island segmentation"""
    is_scale_data: bool = False
    """Indicates whether there was scale information in the data described by ``path`` initially"""


def _get_mask_data(mask_fits_path: Path) -> NDArray[np.bool]:
    """Load data from the target mask file, ensure its pixels are
    only true or false."""
    logger.info(f"Extracting information from {mask_fits_path=}")
    with fits.open(mask_fits_path) as in_fits:
        header = in_fits[0].header
        data = in_fits[0]._data.squeeze()
        is_scale_data = np.max(data) > 1

    norm_data = np.zeros_like(data, dtype=bool)
    norm_data[data > 0] = True

    return FITSMaskData(
        path=mask_fits_path,
        header=header,
        norm_data=norm_data,
        is_scale_data=is_scale_data,
    )


def make_labels(mask_data: NDArray[np.bool]) -> Any:

    labels = label(input=mask_data, structure=np.ones((3, 3)))
    logger.info(f"Number of unique island: {np.max(labels)}")


def create_cublet_regions(fits_mask: FITSMaskNames) -> list[CubletRegion]:

    mask = (
        fits_mask.mask_fits
        if fits_mask.mask_fits is not None
        else fits_mask.scale_msk_fits
    )
    logger.info(f"Will be processing {mask=}")

    fits_mask_data = _get_mask_data(mask_fits_path=mask)
    make_labels(mask_data=fits_mask_data.norm)

    return []
