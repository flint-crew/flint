"""Items related to test functions in the validation stage of flint"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import matplotlib.pyplot as plt
import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from flint.utils import get_packaged_resource_path
from flint.validation import (
    RMSImageInfo,
    SourceCounts,
    calculate_area_correction_per_flux,
    extract_inner_image_array_region,
    get_parser,
    get_rms_image_info,
    get_source_counts,
    make_validator_axes_layout,
    plot_rms_map,
    plot_source_counts,
)


def test_get_parser():
    get_parser()


@pytest.fixture
def rms_path(tmpdir):
    rms_path = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0-MFS-subimage_rms.fits",
        )
    )

    return rms_path


@pytest.fixture
def comp_path(tmpdir):
    comp_path = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB38959.RACS_1357-18.noselfcal.linmos_comp.fits",
        )
    )

    return comp_path


def test_extract_inner_image_array_region():
    """Get the inner region of an image array"""
    shape = (100, 100)
    image = np.arange(np.prod(shape)).reshape(shape)

    sub_image = extract_inner_image_array_region(image=image, fraction=0.5)
    assert sub_image.shape == (50, 50)

    shape = (1, 1, 100, 100)
    image = np.arange(np.prod(shape)).reshape(shape)

    sub_image = extract_inner_image_array_region(image=image, fraction=0.5)
    assert sub_image.shape == (1, 1, 50, 50)

    with pytest.raises(AssertionError):
        extract_inner_image_array_region(image=image, fraction=22.5)


def test_plot_source_counts(comp_path, rms_path):
    """Just a simple test to make sure there are no immediate matplotlib api errors.
    Not testing for actual plotting results."""
    fig, ax = plt.subplots(1, 1)
    comp_table = Table.read(comp_path)
    rms_info = get_rms_image_info(rms_path=rms_path)

    plot_source_counts(catalogue=comp_table, rms_info=rms_info, ax=ax)


def test_source_counts():
    fluxes = np.linspace(0.00003, 0.01, 10000)

    source_counts = get_source_counts(fluxes=fluxes, area=10)

    assert isinstance(source_counts, SourceCounts)
    assert source_counts.area == 10.0


def test_source_counts_with_rms(rms_path):
    fluxes = np.linspace(0.00003, 0.01, 10000)

    source_counts = get_source_counts(fluxes=fluxes, area=10, rms_image_path=rms_path)

    assert isinstance(source_counts, SourceCounts)
    assert source_counts.area == 10.0
    assert isinstance(source_counts.area_fraction, np.ndarray)


def test_calculate_area_correction(rms_path):
    """This is not an entirely robust check as the rms image is only
    10 x 15 pixels. More testing for errors in the calling."""

    flux_bin_centres = np.arange(0.00004, 0.01, 0.0001)

    area_frac = calculate_area_correction_per_flux(
        rms_image_path=rms_path, flux_bin_centre=flux_bin_centres, sigma=3
    )

    test = np.ones(100)
    test[:5] = 0.0

    assert np.allclose(test, area_frac)


def test_rms_image_info(rms_path):
    rms_info = get_rms_image_info(rms_path=rms_path)

    print(rms_info)

    assert isinstance(rms_info, RMSImageInfo)
    assert rms_info.path == rms_path
    assert rms_info.no_valid_pixels == 150
    assert rms_info.shape == (10, 15)
    assert np.isclose(0.0001515522, rms_info.median, atol=1e-5)
    assert np.isclose(0.00015135764, rms_info.minimum, atol=1e-5)
    assert np.isclose(0.0001518184, rms_info.maximum, atol=1e-5)
    assert np.isclose(1.1098655e-07, rms_info.std, atol=1e-5)


def test_rms_map_with_degenerate_axes(rms_path, tmp_path):
    """A map shaped like a wsclean image, as flint's FFT BANE writes it, is read
    and plotted as the same two-dimensional map"""
    header = fits.getheader(rms_path)
    header.update(CTYPE3="FREQ", CRVAL3=1.4e9, CDELT3=1e6, CRPIX3=1.0)
    header.update(CTYPE4="STOKES", CRVAL4=1.0, CDELT4=1.0, CRPIX4=1.0)
    degenerate_path = tmp_path / "rms.fits"
    fits.writeto(degenerate_path, fits.getdata(rms_path)[None, None], header)

    rms_info = get_rms_image_info(rms_path=degenerate_path)
    flat_info = get_rms_image_info(rms_path=rms_path)

    assert rms_info.shape == flat_info.shape == (10, 15)
    # A mode per pixel rather than one value is what broke the statistics table
    assert np.ndim(rms_info.mode) == 0
    assert rms_info.mode == flat_info.mode
    assert rms_info.median == flat_info.median

    fig = plt.figure()
    layout = make_validator_axes_layout(fig=fig, rms_path=degenerate_path)
    plot_rms_map(fig=fig, ax=layout.ax_rms, rms_path=degenerate_path)
    plt.close(fig)


class Example(NamedTuple):
    a: int
    b: str
    c: float


def test_expected_namedtuple_get():
    """This is a simple test to ensure the behaviour of
    NamedTuple.__getattribute__ remains OK. This is currently
    used in the validation plotting to iterate over known
    surveys
    """

    test = Example(a=1, b="123", c=1.23)

    assert test.__getattribute__("a") == 1
    assert test.__getattribute__("b") == "123"
    assert test.__getattribute__("c") == 1.23
