"""Tests around the holography hlper utility"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from flint.misc.holo import (
    FITSCubeInfo,
    FrequencyGrid,
    construct_frequency_grid,
    construct_spatial_output_wcs,
    create_fits_info,
    get_freq_axis,
    get_parser,
    load_and_sort_cubes,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def example_cube_fits(tmpdir) -> tuple[Path, ...]:
    """Load and unzip example cube data"""

    cube_zip = get_packaged_resource_path(
        package="flint.data.tests", filename="test_holo_cubes.zip"
    )

    output_path = Path(tmpdir) / "subset_cubes"
    output_path.mkdir(parents=True, exist_ok=True)

    shutil.unpack_archive(cube_zip, output_path)

    output_cubes = sorted(tuple(output_path.glob("*fits")))
    assert len(output_cubes) == 3

    return tuple(output_cubes)


def test_construct_frequency_grid(example_cube_fits) -> None:
    """Construct the frequnecy grid that would be placed into the
    final output cube"""
    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    frequency_grid = construct_frequency_grid(fits_cube_infos=fits_cube_infos)
    assert isinstance(frequency_grid, FrequencyGrid)
    assert frequency_grid.cdelt == 1000000.0
    assert frequency_grid.min_hz == 799990740.7407407
    assert frequency_grid.max_hz == 1798990740.740741
    assert len(frequency_grid.grid) == 1000


def test_get_freq_axis(example_cube_fits) -> None:
    """Extract the spectral axis from a cube and reconstruct the frequency axis
    as a numpy array, in herta"""
    header = fits.getheader(example_cube_fits[0])

    freqs = get_freq_axis(header=header)
    print(example_cube_fits[0])

    assert freqs[0] == 1151990740.7407405
    assert freqs[19] == 1170990740.7407405
    assert len(freqs) == 288


def test_create_spatual_wcs(example_cube_fits) -> None:
    """Construct the final WCS 2d axis"""
    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    hdr = construct_spatial_output_wcs(fits_cube_infos=fits_cube_infos)
    assert isinstance(hdr, fits.Header)

    assert np.isclose(hdr["CRVAL1"], 6.6871180879046e-21)
    assert np.isclose(hdr["CDELT1"], -0.018502035580794)


def test_load_and_sort_cubes(example_cube_fits) -> None:
    """Test the loader and sorter that forms the FITSCubeInfos"""

    fits_cube_infos = load_and_sort_cubes(cube_paths=example_cube_fits)

    assert isinstance(fits_cube_infos, list)
    assert len(fits_cube_infos) == 3
    assert all(
        isinstance(fits_cube_info, FITSCubeInfo) for fits_cube_info in fits_cube_infos
    )
    # makes sure sorted in ascending order
    assert np.all(
        np.diff([fits_cube_info.freqs_hz[0] for fits_cube_info in fits_cube_infos]) > 0
    )


def test_make_fits_cube_info(example_cube_fits) -> None:
    """Construct an example FITSCubeInfo instance"""

    fits_cube_info = create_fits_info(cube_path=example_cube_fits[0])

    assert isinstance(fits_cube_info, FITSCubeInfo)
    assert fits_cube_info.spatial_shape == (3, 3)


def test_get_parser() -> None:
    """A dumb tester for the argument parser"""
    _ = get_parser()
