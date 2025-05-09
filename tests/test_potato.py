"""Some basic checks around the potato peel functionality"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.table import Table

from flint.imager.wsclean import WSCleanOptions
from flint.options import MS
from flint.peel.potato import (
    NormalisedSources,
    PotatoConfigCommand,
    PotatoConfigOptions,
    PotatoPeelArguments,
    PotatoPeelCommand,
    PotatoPeelOptions,
    _potato_config_command,
    _potato_peel_command,
    create_run_potato_peel,
    find_sources_to_peel,
    get_source_props_from_table,
    load_known_peel_sources,
    source_within_image_fov,
)
from flint.sky_model import (
    AiryResponse,
    GaussianResponse,
    SincSquaredResponse,
    generate_pb,
)

# TODO: NEED TESTS FOR THE POTATO PEEL OPTIONS


def test_source_in_image_fov():
    """Test to see if source is within an image FoV"""
    # This comes from a test wsclean produced image. Not sure
    # why or how a minus crval1 has come out.
    wcs_dict = dict(
        NAXIS1=8128,
        NAXIS2=8128,
        ORIGIN="WSClean",
        CTYPE1="RA---SIN",
        CRPIX1=4065,
        CRVAL1=-1.722664244157e02,
        CDELT1=-6.944444444444e-04,
        CUNIT1="deg",
        CTYPE2="DEC--SIN",
        CRPIX2=4065,
        CRVAL2=2.625771981318e00,
        CDELT2=6.944444444444e-04,
        CUNIT2="deg",
    )
    center_position = SkyCoord(wcs_dict["CRVAL1"] * u.deg, wcs_dict["CRVAL2"] * u.deg)
    known_tato = SkyCoord("12:29:06 02:03:08", unit=(u.hourangle, u.deg))
    outside_fov = SkyCoord("12:29:06 10:03:08", unit=(u.hourangle, u.deg))

    in_image = source_within_image_fov(
        source_coord=known_tato,
        beam_coord=center_position,
        image_size=wcs_dict["NAXIS1"],
        pixel_scale=wcs_dict["CRVAL1"] * u.deg,
    )

    assert in_image == True  # noqa

    outside_strs = (
        ("12:29:06 10:03:08", False),
        ("12:29:06 8:03:08", False),
        ("12:29:06 6:03:08", False),
        ("12:29:06 5:30:08", False),
        ("12:29:06 5:27:08", False),
        ("12:29:06 5:26:08", True),
    )

    for outside_str, outside_value in outside_strs:
        outside_fov = SkyCoord(outside_str, unit=(u.hourangle, u.deg))
        out_image = source_within_image_fov(
            source_coord=outside_fov,
            beam_coord=center_position,
            image_size=wcs_dict["NAXIS1"],
            pixel_scale=wcs_dict["CDELT1"] * u.deg,
        )

        assert out_image == outside_value


def test_potato_peel_command(ms_example):
    """Test to see if the potato peel command can be generated correctly"""
    ms = MS(path=ms_example, column="DATA")

    # Set up the sources to peel out
    image_options = WSCleanOptions(size=8000, scale="2.5arcsec")
    sources = find_sources_to_peel(ms=ms, image_options=image_options)
    source_props = get_source_props_from_table(table=sources)

    potato_peel_arguments = PotatoPeelArguments(
        ms=ms.path,
        ras=source_props.source_ras,
        decs=source_props.source_decs,
        peel_fovs=source_props.source_fovs,
        n=source_props.source_names,
        image_fov=1.0,
    )
    potato_peel_options = PotatoPeelOptions()

    peel_command = _potato_peel_command(
        ms=ms,
        potato_peel_arguments=potato_peel_arguments,
        potato_peel_options=potato_peel_options,
    )

    assert isinstance(peel_command, PotatoPeelCommand)
    assert peel_command.ms.path == ms.path
    assert isinstance(peel_command.command, str)

    # expected_command = f"hot_potato {str(ms.path)} 1.0000 --ras 83.82499999999999 79.94999999999999 --decs -5.386388888888889 -45.764722222222225 --peel_fovs 0.11850000000000001 0.105 -n Orion_A Pictor_A -solint 30 -calmode P -minpeelflux 0.5 -refant 1 --direct_subtract --intermediate_peels -T peel "
    # assert peel_command.command == expected_command


def test_potato_config_command():
    a = PotatoConfigOptions()
    ex = Path("This/example/SB1234.potato.config")

    command = _potato_config_command(config_path=ex, potato_config_options=a)
    # expected = "peel_configuration.py This/example/SB1234.potato.config --image_size 6148 --image_scale 0.0006944 --image_briggs -1.5 --image_channels 4 --image_minuvl 0.0 --peel_size 1000 --peel_scale 0.0006944 --peel_channels 16 --peel_nmiter 7 --peel_minuvl 0.0 --peel_multiscale "

    assert isinstance(command, PotatoConfigCommand)
    # assert command.command == expected
    assert command.config_path == ex


def test_normalised_sources_to_peel(ms_example):
    """See whether the normalisation of sources in a table for potato CLI works"""
    image_options = WSCleanOptions(size=8000, scale="2.5arcsec")

    sources = find_sources_to_peel(ms=ms_example, image_options=image_options)

    source_props = get_source_props_from_table(table=sources)

    assert isinstance(source_props, NormalisedSources)
    assert isinstance(source_props.source_ras, tuple)
    assert isinstance(source_props.source_decs, tuple)
    assert isinstance(source_props.source_fovs, tuple)
    assert isinstance(source_props.source_names, tuple)

    assert len(source_props.source_ras) == 3
    assert len(source_props.source_decs) == 3
    assert len(source_props.source_fovs) == 3
    assert len(source_props.source_names) == 3


def test_check_sources_to_peel(ms_example):
    """See whether the peeling constraints work"""
    image_options = WSCleanOptions(size=8128, scale="2.5arcsec")

    sources = find_sources_to_peel(ms=ms_example, image_options=image_options)

    known_sources = load_known_peel_sources()

    assert len(known_sources) > len(sources)

    # See the WCS constructed in the above
    center_position = SkyCoord(-1.722664244157e02 * u.deg, 2.625771981318e00 * u.deg)
    sources = find_sources_to_peel(
        ms=ms_example,
        image_options=image_options,
        override_beam_position_with=center_position,
    )

    assert len(sources) == 3
    assert sources["Name"][0] == "VirA"


def test_load_peel_sources():
    """Ensure we can load in the reference set of sources for peeling"""

    tab = load_known_peel_sources()
    assert isinstance(tab, Table)
    assert len(tab) > 4


def test_pb_models():
    """See if the responses can be loaded and executed"""
    freq = 1.0 * u.GHz
    aperture = 12 * u.m
    offset = np.arange(0, 200) * u.arcmin

    pb_gauss = generate_pb(
        pb_type="gaussian", freqs=freq, aperture=aperture, offset=offset
    )
    pb_sinc = generate_pb(
        pb_type="sincsquared", freqs=freq, aperture=aperture, offset=offset
    )
    pb_airy = generate_pb(pb_type="airy", freqs=freq, aperture=aperture, offset=offset)

    assert isinstance(pb_gauss, GaussianResponse)
    assert isinstance(pb_sinc, SincSquaredResponse)
    assert isinstance(pb_airy, AiryResponse)

    assert np.isclose(pb_gauss.atten[0], 1.0)
    assert np.isclose(pb_sinc.atten[0], 1.0)
    assert np.isclose(pb_airy.atten[0], 1.0)

    for m in (pb_gauss, pb_sinc, pb_airy):
        assert m.freqs == freq

    # Following known values were generated with the following
    # code, verified, and placed here
    """
    freq = 1.0 * u.GHz
    aperture = 12 * u.m
    offset = np.arange(0, 200) * u.arcmin

    pb_gauss = generate_pb(
        pb_type="gaussian", freqs=freq, aperture=aperture, offset=offset
    )
    pb_sinc = generate_pb(
        pb_type="sincsquared", freqs=freq, aperture=aperture, offset=offset
    )
    pb_airy = generate_pb(pb_type="airy", freqs=freq, aperture=aperture, offset=offset)
    """

    assert np.allclose(
        pb_gauss.atten[10:15],
        [0.96310886, 0.95553634, 0.94731093, 0.93845055, 0.9289744],
    )
    assert np.allclose(
        pb_sinc.atten[10:15],
        [0.96516646, 0.95797617, 0.95015023, 0.94170175, 0.93264483],
    )
    assert np.allclose(
        pb_airy.atten[10:15],
        [0.96701134, 0.96020018, 0.95278619, 0.94478158, 0.93619952],
    )

    assert np.allclose(
        pb_gauss.atten[140:145],
        [0.0006315, 0.0005682, 0.00051086, 0.00045896, 0.00041203],
    )
    assert np.allclose(
        pb_sinc.atten[140:145],
        [0.04699694, 0.04675459, 0.04641816, 0.04599033, 0.04547408],
    )
    assert np.allclose(
        pb_airy.atten[140:145],
        [0.01749399, 0.01748965, 0.01743916, 0.01734373, 0.01720478],
    )


def test_bad_pb_model():
    """Makes sure ValueError raised on unknown model"""
    freq = 1.0 * u.GHz
    aperture = 12 * u.m
    offset = np.arange(0, 200) * u.arcmin

    with pytest.raises(ValueError):
        generate_pb(
            pb_type="ThisDoesNotExist", freqs=freq, aperture=aperture, offset=offset
        )


def test_create_run_potato_peel_with_custom_T(tmp_path, ms_example, monkeypatch):
    """Exercise create_run_potato_peel: T override, directory creation and singularity run."""

    # set up a fake container and MS
    potato_container = tmp_path / "potato.sif"
    potato_container.write_text("")  # doesn't actually get used
    ms = MS(path=ms_example, column="DATA")

    # build arguments exactly as in the peel command tests
    image_opts = WSCleanOptions(size=8000, scale="2.5arcsec")
    sources = find_sources_to_peel(ms=ms, image_options=image_opts)
    props = get_source_props_from_table(table=sources)
    peel_args = PotatoPeelArguments(
        ms=ms.path,
        ras=props.source_ras,
        decs=props.source_decs,
        peel_fovs=props.source_fovs,
        n=props.source_names,
        image_fov=1.0,
    )

    # choose a T that doesn't exist yet
    tdir = tmp_path / "peel_temp"
    peel_opts = PotatoPeelOptions(T=str(tdir))

    # capture create_directory calls
    created = []
    monkeypatch.setattr(
        "flint.peel.potato.create_directory",
        lambda directory: created.append(directory),
    )

    # capture run_singularity_command calls
    runs = []
    monkeypatch.setattr(
        "flint.peel.potato.run_singularity_command",
        lambda image, command, bind_dirs: runs.append(
            {
                "image": image,
                "command": command,
                "bind_dirs": bind_dirs,
            }
        ),
    )

    # call the function under test
    cmd = create_run_potato_peel(
        potato_container=potato_container,
        ms=ms,
        potato_peel_arguments=peel_args,
        potato_peel_options=peel_opts,
    )

    # returned command must match what _potato_peel_command produces
    assert isinstance(cmd, PotatoPeelCommand)
    assert runs, "run_singularity_command was not invoked"

    # directory should have been created exactly once
    assert created == [Path(peel_opts.T)]

    # verify bind_dirs passed into the run call
    run_call = runs[0]
    bd = run_call["bind_dirs"]
    assert Path(ms.path) in bd
    assert Path(ms.path).parent in bd
    assert Path(peel_opts.T) in bd

    # verify singularity invocation was with our container and the generated command
    assert run_call["image"] == potato_container
    assert run_call["command"] == cmd.command
