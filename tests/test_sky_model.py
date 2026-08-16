"""Tests around the sky-model code"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy import units as u
from astropy.table import Table
from capn_crunch import create_options_from_parser

import flint.sky_model as sky_model_module
from flint.sky_model import (
    SkyModel,
    SkyModelOptions,
    SkyModelOutputPaths,
    create_sky_model,
    get_parser,
    get_sky_model_output_paths,
    load_user_catalogue,
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


def test_extracting_sky_model_user_catalogue(ms_example_and_nvss):
    """create_sky_model via a user-supplied catalogue_path (pointed at the same
    NVSS.fits, with its known column names given explicitly) should behave the
    same as the known-catalogue reference_name path"""
    ms_path, nvss_path = ms_example_and_nvss

    sky_model_options = SkyModelOptions(
        catalogue_path=nvss_path,
        catalogue_freq=1.4e9,
        catalogue_ra_col="_RAJ2000",
        catalogue_dec_col="_DEJ2000",
        catalogue_name_col="NVSS",
        catalogue_flux_col="S1.4",
        catalogue_flux_unit="mJy",
        catalogue_maj_col="MajAxis",
        catalogue_min_col="MinAxis",
        catalogue_pa_col="PA",
        catalogue_sizes_deconvolved=True,
    )
    sky_model = create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert isinstance(sky_model, SkyModel)
    assert sky_model.no_sources == 18


def test_create_sky_model_uses_holography_when_supplied(
    ms_example_and_nvss, monkeypatch
):
    """When holofile is set, create_sky_model should sample the holography beam
    instead of the idealized Gaussian PB"""
    ms_path, nvss_path = ms_example_and_nvss

    calls = []

    def _fake_attenuation(holofile, beam, position, freqs):
        calls.append((holofile, beam))
        return np.ones(len(freqs))

    monkeypatch.setattr(sky_model_module, "sample_beam_attenuation", _fake_attenuation)

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
    )
    holofile = Path("fake_holo.fits")
    sky_model = create_sky_model(
        ms_path=ms_path, sky_model_options=sky_model_options, holofile=holofile
    )

    assert isinstance(sky_model, SkyModel)
    assert len(calls) > 0
    assert all(h == holofile and b == 0 for h, b in calls)


def test_load_user_catalogue_requires_ra_dec_flux(tmp_path):
    """ra/dec/flux columns are never guessed for a user catalogue"""
    cat_path = tmp_path / "cat.fits"
    Table({"ra": [10.0], "dec": [-30.0], "flux": [1.0]}).write(cat_path)

    sky_model_options = SkyModelOptions(catalogue_path=cat_path, catalogue_freq=1e9)
    with pytest.raises(ValueError):
        load_user_catalogue(sky_model_options=sky_model_options)


def test_load_user_catalogue_shape_cols_all_or_nothing(tmp_path):
    """maj/min/pa columns must be all set or all unset"""
    cat_path = tmp_path / "cat.fits"
    Table({"ra": [10.0], "dec": [-30.0], "flux": [1.0], "maj": [5.0]}).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq=1e9,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
        catalogue_maj_col="maj",
    )
    with pytest.raises(ValueError):
        load_user_catalogue(sky_model_options=sky_model_options)


def test_load_user_catalogue_requires_freq_or_freq_col(tmp_path):
    """One of catalogue_freq/catalogue_freq_col must be set"""
    cat_path = tmp_path / "cat.fits"
    Table({"ra": [10.0], "dec": [-30.0], "flux": [1.0]}).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
    )
    with pytest.raises(ValueError):
        load_user_catalogue(sky_model_options=sky_model_options)


def test_load_user_catalogue_requires_sizes_deconvolved_flag(tmp_path):
    """catalogue_sizes_deconvolved must be set whenever shape columns are given"""
    cat_path = tmp_path / "cat.fits"
    Table(
        {
            "ra": [10.0],
            "dec": [-30.0],
            "flux": [1.0],
            "maj": [10.0],
            "min": [8.0],
            "pa": [0.0],
        }
    ).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq=1e9,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
        catalogue_maj_col="maj",
        catalogue_min_col="min",
        catalogue_pa_col="pa",
    )
    with pytest.raises(ValueError):
        load_user_catalogue(sky_model_options=sky_model_options)


def test_load_user_catalogue_requires_psf_cols_when_not_deconvolved(tmp_path):
    """PSF columns are required when catalogue_sizes_deconvolved is False"""
    cat_path = tmp_path / "cat.fits"
    Table(
        {
            "ra": [10.0],
            "dec": [-30.0],
            "flux": [1.0],
            "maj": [10.0],
            "min": [8.0],
            "pa": [0.0],
        }
    ).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq=1e9,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
        catalogue_maj_col="maj",
        catalogue_min_col="min",
        catalogue_pa_col="pa",
        catalogue_sizes_deconvolved=False,
    )
    with pytest.raises(ValueError):
        load_user_catalogue(sky_model_options=sky_model_options)


def test_load_user_catalogue_deconvolves_shapes(tmp_path):
    """As-observed sizes are deconvolved by their per-row PSF"""
    from radio_beam import Beam

    cat_path = tmp_path / "cat.fits"
    Table(
        {
            "ra": [10.0],
            "dec": [-30.0],
            "flux": [1.0],
            "maj": [15.0],
            "min": [15.0],
            "pa": [0.0],
            "psf_maj": [10.0],
            "psf_min": [10.0],
            "psf_pa": [0.0],
        }
    ).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq=1e9,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
        catalogue_maj_col="maj",
        catalogue_min_col="min",
        catalogue_pa_col="pa",
        catalogue_sizes_deconvolved=False,
        catalogue_psf_maj_col="psf_maj",
        catalogue_psf_min_col="psf_min",
        catalogue_psf_pa_col="psf_pa",
    )
    catalogue, table = load_user_catalogue(sky_model_options=sky_model_options)

    expected = Beam(
        major=15.0 * u.arcsec, minor=15.0 * u.arcsec, pa=0 * u.deg
    ).deconvolve(Beam(major=10.0 * u.arcsec, minor=10.0 * u.arcsec, pa=0 * u.deg))
    assert np.isclose(table[catalogue.maj_col][0], expected.major.to(u.arcsec).value)
    assert np.isclose(table[catalogue.min_col][0], expected.minor.to(u.arcsec).value)


def test_load_user_catalogue_per_row_freq_col(tmp_path):
    """catalogue_freq_col carries a per-row reference frequency, in the units given"""
    cat_path = tmp_path / "cat.fits"
    Table(
        {
            "ra": [10.0, 20.0],
            "dec": [-30.0, -31.0],
            "flux": [1.0, 2.0],
            "ref_freq_mhz": [800.0, 900.0],
        }
    ).write(cat_path)

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq_col="ref_freq_mhz",
        catalogue_freq_unit="MHz",
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
    )
    catalogue, table = load_user_catalogue(sky_model_options=sky_model_options)

    assert np.allclose(table["ref_freq_mhz"].quantity.to(u.Hz).value, [800e6, 900e6])


def test_load_user_catalogue_point_source_default(tmp_path):
    """Omitting the shape columns defaults every source to a point source, and
    omitting the name column synthesises one"""
    cat_path = tmp_path / "cat.fits"
    Table({"ra": [10.0, 20.0], "dec": [-30.0, -31.0], "flux": [1.0, 2.0]}).write(
        cat_path
    )

    sky_model_options = SkyModelOptions(
        catalogue_path=cat_path,
        catalogue_freq=1e9,
        catalogue_ra_col="ra",
        catalogue_dec_col="dec",
        catalogue_flux_col="flux",
    )
    catalogue, table = load_user_catalogue(sky_model_options=sky_model_options)

    assert catalogue.survey == "USER"
    assert np.all(table[catalogue.maj_col] == 0)
    assert np.all(table[catalogue.min_col] == 0)
    assert np.all(table[catalogue.pa_col] == 0)
    assert list(table[catalogue.name_col]) == ["src0", "src1"]
    assert np.allclose(table["alpha"], sky_model_options.assumed_alpha)
    assert np.allclose(table["beta"], sky_model_options.assumed_q)


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
