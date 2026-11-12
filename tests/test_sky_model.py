"""Tests around the sky-model code"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import MaskedColumn, QTable, Table
from capn_crunch import create_options_from_parser

import flint.sky_model as sky_model_module
from flint.catalogue import Catalogue
from flint.exceptions import UnusableBeamError
from flint.misc.holo import BeamResponse
from flint.sky_model import (
    CurvedPL,
    SkyModel,
    SkyModelOptions,
    SkyModelOutputPaths,
    create_sky_model,
    get_beam_number,
    get_parser,
    get_sky_model_output_paths,
    load_user_catalogue,
    make_calibrate_model,
    phase_centre_pa_offset,
    preprocess_catalogue,
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


def _beam_response_over(freqs_hz: list[float]) -> BeamResponse:
    """A flat beam response whose usable planes sit at `freqs_hz`"""
    return BeamResponse(
        freqs_hz=np.array(freqs_hz),
        planes=np.ones((len(freqs_hz), 5, 5)),
        pixel_scale_rad=(-1e-3, 1e-3),
        centre_pixel=(2.0, 2.0),
    )


@pytest.mark.parametrize("taper", [False, True])
def test_create_sky_model_uses_holography_when_supplied(
    ms_example_and_nvss, monkeypatch, taper
):
    """When holofile is set, the beam named in the MS is loaded once and sampled
    for every source, with holography_taper passed through"""
    ms_path, nvss_path = ms_example_and_nvss

    response = _beam_response_over([1e8, 1e10])
    loads = []
    samples = []

    def _fake_load(holofile, beam):
        loads.append((holofile, beam))
        return response

    def _fake_sample(response, beam_centre, position, freqs, alpha, taper):
        samples.append((response, taper))
        return np.ones(len(freqs))

    monkeypatch.setattr(sky_model_module, "load_beam_response", _fake_load)
    monkeypatch.setattr(sky_model_module, "sample_beam_response", _fake_sample)

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
        holography_taper=taper,
    )
    holofile = Path("fake_holo.fits")
    sky_model = create_sky_model(
        ms_path=ms_path, sky_model_options=sky_model_options, holofile=holofile
    )

    assert isinstance(sky_model, SkyModel)
    assert loads == [(holofile, 0)]
    assert len(samples) > 0
    assert all(r is response and t == taper for r, t in samples)


def test_create_sky_model_warns_about_channels_outside_planes(
    ms_example_and_nvss, monkeypatch, caplog
):
    """Channels beyond the holography's usable planes are reported once"""
    ms_path, nvss_path = ms_example_and_nvss

    monkeypatch.setattr(
        sky_model_module,
        "load_beam_response",
        lambda holofile, beam: _beam_response_over([1e8]),
    )
    monkeypatch.setattr(
        sky_model_module,
        "sample_beam_response",
        lambda response, beam_centre, position, freqs, alpha, taper: np.ones(
            len(freqs)
        ),
    )

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
    )
    with caplog.at_level(logging.WARNING):
        create_sky_model(
            ms_path=ms_path,
            sky_model_options=sky_model_options,
            holofile=Path("fake_holo.fits"),
        )

    assert caplog.text.count("outside the usable holography planes") == 1


def test_create_sky_model_unusable_beam_falls_back_to_gaussian(
    ms_example_and_nvss, monkeypatch
):
    """A holography beam with no usable response falls back to the Gaussian beam"""
    ms_path, nvss_path = ms_example_and_nvss

    def _fake_load(holofile, beam):
        raise UnusableBeamError("no usable response")

    def _fake_sample(**kwargs):
        raise AssertionError("an unusable beam should not be sampled")

    monkeypatch.setattr(sky_model_module, "load_beam_response", _fake_load)
    monkeypatch.setattr(sky_model_module, "sample_beam_response", _fake_sample)

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
    )
    sky_model = create_sky_model(
        ms_path=ms_path,
        sky_model_options=sky_model_options,
        holofile=Path("fake_holo.fits"),
    )

    assert isinstance(sky_model, SkyModel)
    assert sky_model.no_sources == 18


@pytest.mark.parametrize("rotate", [False, True])
def test_create_sky_model_passes_phase_centre_for_pa(
    ms_example_and_nvss, monkeypatch, rotate
):
    """rotate_pa_to_phase_centre hands the phase centre to make_calibrate_model"""
    ms_path, nvss_path = ms_example_and_nvss

    phase_centres = []

    def _fake_make_calibrate_model(out_path, sources, phase_centre):
        phase_centres.append(phase_centre)
        return out_path

    monkeypatch.setattr(
        sky_model_module, "make_calibrate_model", _fake_make_calibrate_model
    )

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
        write_calibrate_model=True,
        rotate_pa_to_phase_centre=rotate,
    )
    create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert len(phase_centres) == 1
    if rotate:
        assert isinstance(phase_centres[0], SkyCoord)
    else:
        assert phase_centres[0] is None


def _calibrate_sources() -> list[tuple]:
    """One Gaussian and one point source, ready for make_calibrate_model"""
    table = QTable(
        {
            "name": ["gaus", "point"],  # codespell:ignore gaus
            "RA": [159.4, 157.7] * u.deg,
            "DEC": [18.3, 18.8] * u.deg,
            "maj": [30.0, 0.0] * u.arcsec,
            "min": [10.0, 0.0] * u.arcsec,
            "pa": [40.0, 0.0] * u.deg,
        }
    )
    model = CurvedPL(norm=1.0, alpha=-0.8, beta=0.0, ref_nu=1e9)
    return [(row, model) for row in table]


def _read_calibrate_shapes(model_path: Path) -> dict[str, list[str]]:
    """The major axis, minor axis and orientation fields of each component"""
    lines = model_path.read_text().splitlines()[1:]
    # Fields after the bracketed spectral terms: LogarithmicSI, reference
    # frequency, then the three shape fields
    return {line.split(",")[0]: line.split("]")[1].split(",")[3:6] for line in lines}


def test_make_calibrate_model_writes_minor_axis(tmp_path):
    """Gaussian components keep their own minor axis and position angle"""
    model_path = make_calibrate_model(
        out_path=tmp_path / "model.txt", sources=_calibrate_sources()
    )

    shapes = _read_calibrate_shapes(model_path)
    assert [float(v) for v in shapes["gaus"]] == [  # codespell:ignore gaus
        30.0,
        10.0,
        40.0,
    ]
    assert shapes["point"] == ["", "", ""]


def test_preprocess_catalogue_makes_sources_without_pa_points():
    """Blank PAs, NaN or masked as in NVSS, become points with zero-size axes"""
    table = Table(
        {
            "ra": [98.2, 98.3, 98.4] * u.deg,
            "dec": [-30.9, -30.8, -30.7] * u.deg,
            "name": ["resolved", "nan_pa", "masked_pa"],
            "flux": [1.0, 1.0, 1.0] * u.Jy,
            "maj": [30.0, 18.0, 20.0] * u.arcsec,
            "min": [10.0, 15.0, 19.0] * u.arcsec,
            "pa": MaskedColumn(
                [40.0, np.nan, 10.0], mask=[False, False, True], unit=u.deg
            ),
            "alpha": [-0.8, -0.8, -0.8],
            "beta": [0.0, 0.0, 0.0],
        }
    )
    catalogue = Catalogue(
        survey="TEST",
        file_name="test.fits",
        freq=1.4e9,
        ra_col="ra",
        dec_col="dec",
        name_col="name",
        flux_col="flux",
        maj_col="maj",
        min_col="min",
        pa_col="pa",
        alpha_col="alpha",
        beta_col="beta",
    )

    processed = preprocess_catalogue(
        catalogue,
        table,
        ms_pointing=SkyCoord(98.3 * u.deg, -30.8 * u.deg),
        radial_cut=1 * u.deg,
    )

    np.testing.assert_allclose(processed["maj"].to_value(u.arcsec), [30.0, 0.0, 0.0])
    np.testing.assert_allclose(processed["min"].to_value(u.arcsec), [10.0, 0.0, 0.0])
    np.testing.assert_allclose(processed["pa"].to_value(u.deg), [40.0, 0.0, 0.0])


def test_sky_model_outputs_have_no_nan(ms_example_and_nvss):
    """NVSS sources with a blank PA reach no model file as NaN"""
    ms_path, nvss_path = ms_example_and_nvss

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
        write_calibrate_model=True,
        write_hyperdrive_model=True,
        write_ds9_region=True,
    )
    sky_model = create_sky_model(ms_path=ms_path, sky_model_options=sky_model_options)

    assert isinstance(sky_model, SkyModel)
    for path in (
        sky_model.calibrate_model,
        sky_model.hyperdrive_model,
        sky_model.ds9_region,
    ):
        text = path.read_text().lower()
        assert "nan" not in text
        assert "—" not in text
    assert ",POINT," in sky_model.calibrate_model.read_text()


def test_get_beam_number_falls_back_to_feed1(ms_example_and_nvss, caplog):
    """A name without a beam number falls back to FEED1, with a clear warning"""
    ms_path, _ = ms_example_and_nvss
    assert get_beam_number(ms_path=ms_path) == 0

    renamed = ms_path.rename(ms_path.with_name("renamed.ms"))
    with caplog.at_level(logging.WARNING):
        assert get_beam_number(ms_path=renamed) == 0

    assert "BEAM NUMBER NOT IN NAME" in caplog.text


def test_create_sky_model_raises_when_beam_unknown(ms_example_and_nvss, monkeypatch):
    """With no beam in the name or FEED1, the error is raised, not hidden by a Gaussian fallback"""
    ms_path, nvss_path = ms_example_and_nvss
    renamed = ms_path.rename(ms_path.with_name("renamed.ms"))

    def _fake_get_beam_from_ms(ms):
        raise AssertionError("more than one beam")

    monkeypatch.setattr(sky_model_module, "get_beam_from_ms", _fake_get_beam_from_ms)

    sky_model_options = SkyModelOptions(
        reference_catalogue_directory=nvss_path.parent,
        reference_name="NVSS",
    )
    with pytest.raises(AssertionError):
        create_sky_model(
            ms_path=renamed,
            sky_model_options=sky_model_options,
            holofile=Path("fake_holo.fits"),
        )


def test_make_calibrate_model_rotates_pa_to_phase_centre(tmp_path):
    """With a phase centre, Gaussian position angles are measured from its north"""
    phase_centre = SkyCoord(157.70551 * u.deg, 18.25744 * u.deg)
    sources = _calibrate_sources()

    model_path = make_calibrate_model(
        out_path=tmp_path / "model.txt", sources=sources, phase_centre=phase_centre
    )

    shapes = _read_calibrate_shapes(model_path)
    offset = phase_centre_pa_offset(
        phase_centre=phase_centre,
        position=SkyCoord(sources[0][0]["RA"], sources[0][0]["DEC"]),
    )
    assert offset.to(u.deg).value < -0.1
    assert np.isclose(
        float(shapes["gaus"][2]),  # codespell:ignore gaus
        40.0 + offset.to(u.deg).value,
    )
    assert shapes["point"] == ["", "", ""]


@pytest.mark.parametrize(
    "dec, pa, expected",
    [
        (18.25744, 0, 0.0),
        (18.25744, 180, 0.0),
        (-80.0, 0, 0.0),
        (18.25744, 90, -0.56),
        (-60.0, 90, 2.94),
        (-80.0, 90, 9.55),
    ],
)
def test_phase_centre_pa_offset(dec, pa, expected):
    """Zero due north or south of the phase centre, growing towards the pole"""
    phase_centre = SkyCoord(157.70551 * u.deg, dec * u.deg)
    position = phase_centre.directional_offset_by(pa * u.deg, 1.7 * u.deg)

    offset = phase_centre_pa_offset(phase_centre=phase_centre, position=position)

    assert np.isclose(offset.to(u.deg).value, expected, atol=0.01)


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
    assert np.allclose(table["beta"], sky_model_options.assumed_beta)


def test_get_working_parser():
    """Make sure that the interaction with the SkyModelOptions
    and the argument parser works"""
    parser = get_parser()

    args = parser.parse_args("example.ms".split())
    ms = Path(args.ms)
    assert ms == Path("example.ms")
    assert args.holofile is None

    args = parser.parse_args("example.ms --holofile holo.fits".split())
    assert args.holofile == Path("holo.fits")

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
