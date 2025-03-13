"""Tests around the sky-model code"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from flint.options import create_options_from_parser
from flint.sky_model import (
    SkyModel,
    SkyModelOptions,
    SkyModelOutputPaths,
    create_sky_model,
    get_parser,
    get_sky_model_output_paths,
)
from flint.utils import get_packaged_resource_path


@pytest.fixture
def ms_example_and_nvss(tmpdir):
    ms_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="SB39400.RACS_0635-31.beam0.small.ms.zip",
        )
    )
    outpath = Path(tmpdir) / "39400"

    shutil.unpack_archive(ms_zip, outpath)

    nvss_zip = Path(
        get_packaged_resource_path(
            package="flint.data.tests",
            filename="NVSS.fits.zip",
        )
    )

    shutil.unpack_archive(nvss_zip, outpath)

    ms_path = Path(outpath) / "SB39400.RACS_0635-31.beam0.small.ms"
    nvss_path = Path(outpath) / "NVSS.fits"

    return ms_path, nvss_path


def test_extracting_sky_model(ms_example_and_nvss):
    """Run the whole sky model creating to make sure nothing has
    broken"""
    ms_path, nvss_path = ms_example_and_nvss
    assert ms_path.exists()
    assert nvss_path.exists()

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
    )
    sky_model = create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert isinstance(sky_model, SkyModel)
    assert sky_model.no_sources == 18
    assert sky_model.calibrate_model is None
    assert sky_model.hyperdrive_model is None
    assert sky_model.ds9_region is None

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
        write_calibrate_model=True,
        write_hyperdrive_model=True,
        write_ds9_region=True,
    )
    sky_model = create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert isinstance(sky_model, SkyModel)
    assert sky_model.no_sources == 18
    assert (
        isinstance(sky_model.calibrate_model, Path)
        and sky_model.calibrate_model.exists()
    )
    assert (
        isinstance(sky_model.hyperdrive_model, Path)
        and sky_model.hyperdrive_model.exists()
    )
    assert isinstance(sky_model.ds9_region, Path) and sky_model.ds9_region.exists()
    assert np.isclose(sky_model.flux_jy, 1.3768, rtol=0.01)


def test_extracting_sky_model_with_none(ms_example_and_nvss):
    """Run the whole sky model creating to make sure nothing has
    broken. Make sure though that no sources are reported and a
    None is returned instead"""
    ms_path, nvss_path = ms_example_and_nvss
    assert ms_path.exists()
    assert nvss_path.exists()

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
        flux_cutoff=100000,
    )
    sky_model = create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert sky_model is None


def test_get_working_parser():
    """Make sure that the interaction with the SkyModelOptions
    and the argument parser works"""
    parser = get_parser()

    args = parser.parse_args("example.ms".split())
    ms = Path(args.ms)
    assert ms == Path("example.ms")

    sky_model_options = create_options_from_parser(
        parser_namespace=args, options_class=SkyModelOptions
    )
    assert isinstance(sky_model_options, SkyModelOptions)
    assert isinstance(sky_model_options.reference_catalogue_directory, Path)


def test_get_sky_model_output_names():
    """Ensure the names are what we expect them to be"""

    ms_path = Path("JackSparrowData.ms")

    sky_model_output_paths = get_sky_model_output_paths(ms_path=ms_path)
    assert isinstance(sky_model_output_paths, SkyModelOutputPaths)
    assert sky_model_output_paths.hyperdrive_path == Path(
        "JackSparrowData.hyperdrive.yaml"
    )
    assert sky_model_output_paths.calibrate_path == Path(
        "JackSparrowData.calibrate.txt"
    )

    with pytest.raises(ValueError):
        get_sky_model_output_paths(ms_path=Path("JackBeNoMS.txt"))
