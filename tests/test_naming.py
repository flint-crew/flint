"""Some tests related to components around measurement sets."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PosixPath

import pytest

from flint.exceptions import NamingException
from flint.naming import (
    CASDANameComponents,
    FITSMaskNames,
    ProcessedNameComponents,
    RawNameComponents,
    _rename_linear_to_stokes,
    add_timestamp_to_path,
    casda_ms_format,
    create_fits_mask_names,
    create_image_cube_name,
    create_imaging_name_prefix,
    create_linmos_base_path,
    create_linmos_names,
    create_ms_name,
    create_name_from_common_fields,
    create_path_from_processed_name_components,
    extract_beam_from_name,
    extract_components_from_name,
    get_aocalibrate_output_path,
    get_beam_resolution_str,
    get_fits_cube_from_paths,
    get_potato_output_base_path,
    get_sbid_from_path,
    get_selfcal_ms_name,
    processed_ms_format,
    raw_ms_format,
    rename_linear_to_stokes,
    split_images,
    update_beam_resolution_field_in_path,
)
from flint.options import MS


def test_create_path_from_process_named_components():
    """Make sure we can create a name"""
    components = ProcessedNameComponents(
        sbid="39400", field="RACS_0000-123", beam="33", spw=None, round="3", pol="i"
    )
    assert isinstance(components, ProcessedNameComponents)

    ex = Path("SB39400.RACS_0000-123.beam33.round3.i")
    parent = Path("Jack/Sparrow/Pirate/King")
    out1 = create_path_from_processed_name_components(
        processed_name_components=components
    )
    out2 = create_path_from_processed_name_components(
        processed_name_components=components, parent_path=parent
    )
    assert isinstance(out1, Path)
    assert isinstance(out2, Path)
    assert ex == out1
    assert parent / ex == out2

    components = ProcessedNameComponents(
        sbid="39400", field="RACS_0000-123", beam="33", spw=234, round="3", pol="i"
    )
    assert isinstance(components, ProcessedNameComponents)

    ex = Path("SB39400.RACS_0000-123.beam33.spw234.round3.i")
    parent = Path("Jack/Sparrow/Pirate/King")
    out1 = create_path_from_processed_name_components(
        processed_name_components=components
    )
    out2 = create_path_from_processed_name_components(
        processed_name_components=components, parent_path=parent
    )
    assert isinstance(out1, Path)
    assert isinstance(out2, Path)
    assert ex == out1
    assert parent / ex == out2

    components = ProcessedNameComponents(
        sbid="39400",
        field="RACS_0000-123",
        beam="33",
        spw=234,
        round="3",
        pol="i",
        channel_range=(123, 567),
    )
    assert isinstance(components, ProcessedNameComponents)

    ex = Path("SB39400.RACS_0000-123.beam33.spw234.round3.i.ch0123-0567")
    parent = Path("Jack/Sparrow/Pirate/King")
    out1 = create_path_from_processed_name_components(
        processed_name_components=components
    )
    out2 = create_path_from_processed_name_components(
        processed_name_components=components, parent_path=parent
    )
    assert isinstance(out1, Path)
    assert isinstance(out2, Path)
    assert ex == out1
    assert parent / ex == out2

    components = ProcessedNameComponents(
        sbid="39400",
        field="RACS_0000-123",
        beam="33",
        spw=234,
        round="3",
        pol="i",
        channel_range=(123, 567444),
    )
    assert isinstance(components, ProcessedNameComponents)

    ex = Path("SB39400.RACS_0000-123.beam33.spw234.round3.i.ch0123-567444")
    parent = Path("Jack/Sparrow/Pirate/King")
    out1 = create_path_from_processed_name_components(
        processed_name_components=components
    )
    out2 = create_path_from_processed_name_components(
        processed_name_components=components, parent_path=parent
    )
    assert isinstance(out1, Path)
    assert isinstance(out2, Path)
    assert ex == out1
    assert parent / ex == out2


def test_create_path_from_process_named_components_2():
    """Make sure we can create a name. The one makes sure we can go full circle"""
    parent = Path("Jacccckkkk/Sparrow")
    ex = parent / Path("SB39400.RACS_0000-123.beam33.spw234.round3.i.ch0123-567444")
    pcn = processed_ms_format(in_name=ex)
    out = create_path_from_processed_name_components(
        processed_name_components=pcn, parent_path=parent
    )
    assert ex == out

    parent = Path("Jacccckkkk/Sparrow")
    ex = parent / Path("SB39400.RACS_0000-123.round3.i.ch0123-0444")
    pcn = processed_ms_format(in_name=ex)
    out = create_path_from_processed_name_components(
        processed_name_components=pcn, parent_path=parent
    )
    assert ex == out


def test_create_imaging_name_prefix():
    """Creates the name that will be used for output image
    products"""
    ms = MS.cast(ms=Path("/Jack/Sparrow/SB63789.EMU_1743-51.beam03.round4.ms"))

    name = create_imaging_name_prefix(ms_path=ms.path)
    assert name == "SB63789.EMU_1743-51.beam03.round4"

    for pol in ("I", "i"):
        name = create_imaging_name_prefix(ms_path=ms.path, pol=pol)
        assert name == "SB63789.EMU_1743-51.beam03.round4.i"

        name = create_imaging_name_prefix(
            ms_path=ms.path, pol=pol, channel_range=(100, 108)
        )
        assert name == "SB63789.EMU_1743-51.beam03.round4.i.ch0100-0108"

    name = create_imaging_name_prefix(ms_path=ms.path, channel_range=(100, 108))
    assert name == "SB63789.EMU_1743-51.beam03.round4.ch0100-0108"


def test_get_cube_fits_from_paths():
    """Identify the files that contain the cube field and are fits"""
    files = [
        "SB63789.EMU_1743-51.beam03.round4.i.image.cube.fits",
        "SB63789.EMU_1743-51.beam03.round4.i.image.cube.other.fields.fits",
        "SB63789.EMU_1743-51.beam03.round4.i.MFS.image.optimal.conv.fits",
        "SB63789.EMU_1743-51.beam03.round4.i.MFS.residual.optimal.conv.fits",
        "SB63789.EMU_1743-51.beam03.round4.i.MFS.image.fits",
        "SB63789.EMU_1743-51.beam03.round4.i.MFS.residual.fits",
    ]
    files = [Path(f) for f in files]

    cube_files = get_fits_cube_from_paths(paths=files)

    assert len(cube_files) == 2
    assert cube_files[0] == Path("SB63789.EMU_1743-51.beam03.round4.i.image.cube.fits")
    assert cube_files[1] == Path(
        "SB63789.EMU_1743-51.beam03.round4.i.image.cube.other.fields.fits"
    )


def test_create_image_cube_name():
    """Put together a consistent file cube name"""
    name = create_image_cube_name(
        image_prefix=Path(
            "/jack/sparrow/worst/pirate/flint_fitscube/57222/SB57222.RACS_1141-55.beam10.round3.i"
        ),
        mode="image",
    )
    assert isinstance(name, Path)
    assert name == Path(
        "/jack/sparrow/worst/pirate/flint_fitscube/57222/SB57222.RACS_1141-55.beam10.round3.i.image.cube.fits"
    )

    name = create_image_cube_name(
        image_prefix=Path("./57222/SB57222.RACS_1141-55.beam10.round3.i"),
        mode="residual",
    )
    assert isinstance(name, Path)
    assert name == Path(
        "./57222/SB57222.RACS_1141-55.beam10.round3.i.residual.cube.fits"
    )

    name = create_image_cube_name(
        image_prefix=Path("./57222/SB57222.RACS_1141-55.beam10.round3.i"),
        mode=["residual", "pirate", "imaging"],
    )
    assert isinstance(name, Path)
    assert name == Path(
        "./57222/SB57222.RACS_1141-55.beam10.round3.i.residual.pirate.imaging.cube.fits"
    )
    name = create_image_cube_name(
        image_prefix=Path("./57222/SB57222.RACS_1141-55.beam10.round3.i"),
        mode=["residual", "pirate", "imaging"],
        suffix="jackie",
    )
    assert isinstance(name, Path)
    assert name == Path(
        "./57222/SB57222.RACS_1141-55.beam10.round3.i.residual.pirate.imaging.jackie.cube.fits"
    )
    name = create_image_cube_name(
        image_prefix=Path("./57222/SB57222.RACS_1141-55.beam10.round3.i"),
        mode=["residual", "pirate", "imaging"],
        suffix=["jackie", "boi"],
    )
    assert isinstance(name, Path)
    assert name == Path(
        "./57222/SB57222.RACS_1141-55.beam10.round3.i.residual.pirate.imaging.jackie.boi.cube.fits"
    )


def test_get_beam_resolution_str():
    """Map the known / support modes of beam resolution in file names"""
    assert "raw" == get_beam_resolution_str(mode="raw")
    assert "optimal" == get_beam_resolution_str(mode="optimal")
    assert "fixed" == get_beam_resolution_str(mode="fixed")

    assert "raw!" == get_beam_resolution_str(mode="raw", marker="!")
    assert "optimal?" == get_beam_resolution_str(mode="optimal", marker="?")
    assert "fixed." == get_beam_resolution_str(mode="fixed", marker=".")

    with pytest.raises(ValueError):
        _ = get_beam_resolution_str("Jack")


def test_update_beam_resolution_mode_in_path():
    """Given a path that has a known beam resolution mode in it, update to another"""

    example = Path("SB57516.RACS_0929-81.round4.i.optimal.round4.residual.linmos.fits")
    expected = Path("SB57516.RACS_0929-81.round4.i.fixed.round4.residual.linmos.fits")

    assert expected == update_beam_resolution_field_in_path(
        path=example, original_mode="optimal", updated_mode="fixed"
    )
    assert expected == update_beam_resolution_field_in_path(
        path=example, original_mode="optimal", updated_mode="fixed", marker="."
    )
    with pytest.raises(AssertionError):
        update_beam_resolution_field_in_path(
            path=example, original_mode="fixed", updated_mode="optimal"
        )
        assert expected == update_beam_resolution_field_in_path(
            path=example, original_mode="optimal", updated_mode="fixed", marker="!"
        )
        update_beam_resolution_field_in_path(
            path=Path("JackSparrowCaresNotForBeamResolutions"),
            original_mode="optimal",
            updated_mode="fixed",
        )


def test_casda_ms_format_1934():
    """Checks around the name format form CASDA  considering 1934"""
    exs = [
        "1934.SB40470.beam35.ms",
        Path("1934.SB40470.beam35.ms"),
    ]
    for ex in exs:
        res = casda_ms_format(in_name=ex)
        assert res is not None
        assert res.format == "1934"
        assert res.sbid == 40470
        assert res.beam == "35"


def test_casda_ms_format():
    """Checks around the name format form CASDA"""
    exs = [
        "scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms",
        Path(
            "scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms"
        ),
    ]
    for ex in exs:
        res = casda_ms_format(in_name=ex)
        assert res is not None
        assert isinstance(res, CASDANameComponents)
        assert res.sbid == 40470
        assert res.beam == "35"
        assert res.field == "RACS_1237+00"
        assert res.alias == "RACS_1237+00"
        assert res.format == "scienceData"

    # Confirm None is returned in silly cases
    exs = [
        "scienceData.fdgdfdfg.RACS_1237+00.SBaveraged_cal.leakage.ms",
        "SB12349.RACS_1234+45.ms",
        "SB12349.RACS_1234+45.round2.ms",
    ]
    for ex in exs:
        res = casda_ms_format(in_name=ex)
        assert res is None


def test_self_cal_name_wbeams():
    """Checks around where the self-calibration naming is working, and the
    correct slicing. This test came after the one below and includes the beam
    number as a component to consider"""

    for beam in range(45):
        ms = Path(f"SB12349.RACS_1234+45.beam{beam:02d}.ms")
        e_ms = Path(f"SB12349.RACS_1234+45.beam{beam:02d}.round1.ms")
        out_ms = get_selfcal_ms_name(in_ms_path=ms, round=1)
        assert out_ms == e_ms

    for round in range(1, 5):
        for beam in range(45):
            ms = Path(f"SB12349.RACS_1234+45.beam{beam:02d}.round{round}.ms")
            e_ms = Path(f"SB12349.RACS_1234+45.beam{beam:02d}.round{round + 1}.ms")
            out_ms = get_selfcal_ms_name(in_ms_path=ms, round=round + 1)
            assert out_ms == e_ms


def test_self_cal_name():
    """Checks around where the self-calibration naming is working, and the
    correct slicing"""

    ms = Path("SB12349.RACS_1234+45.ms")
    e_ms = Path("SB12349.RACS_1234+45.round1.ms")
    out_ms = get_selfcal_ms_name(in_ms_path=ms, round=1)
    assert out_ms == e_ms

    ms = Path("SB12349.RACS_1234+45.round1.ms")
    e_ms = Path("SB12349.RACS_1234+45.round2.ms")
    out_ms = get_selfcal_ms_name(in_ms_path=ms, round=2)
    assert out_ms == e_ms

    ms = Path(
        "/some/other/directory/SB12349.RACS_1234+45.verrrrryyylonnnnnnnhhhhh.round1.ms"
    )
    e_ms = Path(
        "/some/other/directory/SB12349.RACS_1234+45.verrrrryyylonnnnnnnhhhhh.round2.ms"
    )
    out_ms = get_selfcal_ms_name(in_ms_path=ms, round=2)
    assert out_ms == e_ms


def test_potato_output_name():
    ms = Path("/some/made/up/path/SB123.Tim.beam33.round2.ms")
    potato = get_potato_output_base_path(ms_path=ms)

    assert isinstance(potato, Path)
    assert potato == Path("/some/made/up/path/SB123.Tim.beam33.potato")

    config = potato.parent / (potato.name + ".config")
    assert config == Path("/some/made/up/path/SB123.Tim.beam33.potato.config")


def test_add_timestamp_to_path():
    """make sure adding a timestamp to a file name works"""
    dd = datetime(2024, 4, 12, 10, 30, 50, 243910)

    example_str = "/test/this/is/filename.txt"
    stamped_path = add_timestamp_to_path(input_path=example_str, timestamp=dd)
    expected = Path("/test/this/is/filename-20240412-103050.txt")

    assert stamped_path == expected

    example_path = Path("/test/this/is/filename.txt")
    stamped_path = add_timestamp_to_path(input_path=example_path, timestamp=dd)

    assert stamped_path == expected

    now_path = add_timestamp_to_path(input_path=example_path)
    assert now_path != expected


def test_create_fits_mask_names_no_signal():
    fits_image = Path("38960/SB38960.RACS_1418-12.noselfcal_linmos.fits")

    result = create_fits_mask_names(fits_image=fits_image, include_signal_path=False)
    assert isinstance(result, FITSMaskNames)
    assert isinstance(result.mask_fits, Path)
    assert result.signal_fits is None
    assert result.mask_fits == Path(
        "38960/SB38960.RACS_1418-12.noselfcal_linmos.mask.fits"
    )


def test_create_fits_mask_names():
    fits_image = Path("38960/SB38960.RACS_1418-12.noselfcal_linmos.fits")

    result = create_fits_mask_names(fits_image=fits_image, include_signal_path=True)
    assert isinstance(result, FITSMaskNames)
    assert isinstance(result.signal_fits, Path)
    assert isinstance(result.mask_fits, Path)
    assert result.signal_fits == Path(
        "38960/SB38960.RACS_1418-12.noselfcal_linmos.signal.fits"
    )
    assert result.mask_fits == Path(
        "38960/SB38960.RACS_1418-12.noselfcal_linmos.mask.fits"
    )


def test_aocalibrate_into_process_ms_format():
    ms = Path("/some/made/up/path/SB123.Tim.beam33.round2.ms")

    outpath = get_aocalibrate_output_path(
        ms_path=ms, include_preflagger=True, include_smoother=True
    )
    named_components = processed_ms_format(in_name=outpath)
    assert isinstance(named_components, ProcessedNameComponents)
    assert named_components.sbid == "123"
    assert named_components.field == "Tim"
    assert named_components.beam == "33"


def test_aocalibrate_naming():
    ms = Path("/some/made/up/path/SB123.Tim.beam33.round2.ms")

    outpath = get_aocalibrate_output_path(
        ms_path=ms, include_preflagger=True, include_smoother=True
    )
    assert outpath == Path(
        "/some/made/up/path/SB123.Tim.beam33.aocalibrate.preflagged.smoothed.bin"
    )

    outpath = get_aocalibrate_output_path(
        ms_path=ms, include_preflagger=True, include_smoother=False
    )
    assert outpath == Path(
        "/some/made/up/path/SB123.Tim.beam33.aocalibrate.preflagged.bin"
    )

    outpath = get_aocalibrate_output_path(
        ms_path=ms, include_preflagger=False, include_smoother=False
    )
    assert outpath == Path("/some/made/up/path/SB123.Tim.beam33.aocalibrate.bin")

    processed_outputs = processed_ms_format(in_name=outpath)
    assert isinstance(processed_outputs, ProcessedNameComponents)

    ms = Path("/some/made/up/path/SB123.Tim.beam33.spw1.round2.ms")

    outpath = get_aocalibrate_output_path(
        ms_path=ms, include_preflagger=True, include_smoother=True
    )
    assert outpath == Path(
        "/some/made/up/path/SB123.Tim.beam33.spw1.aocalibrate.preflagged.smoothed.bin"
    )


def test_get_sbid_from_path():
    example_path = "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0.ms"

    sbid = get_sbid_from_path(path=Path(example_path))
    assert isinstance(sbid, int)
    assert sbid == 39400

    example_path_2 = "/scratch3/gal16b/askap_sbids/39400"
    sbid = get_sbid_from_path(path=Path(example_path_2))
    assert isinstance(sbid, int)
    assert sbid == 39400

    example_path_3 = (
        "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0.averaged.ms"
    )

    sbid = get_sbid_from_path(path=Path(example_path_3))
    assert isinstance(sbid, int)
    assert sbid == 39400


def test_raw_name_components():
    ms = "2022-04-14_100122_0.ms"

    components = raw_ms_format(ms)
    assert isinstance(components, RawNameComponents)
    assert components.beam == "0"
    assert components.date == "2022-04-14"
    assert components.time == "100122"
    assert components.spw is None

    ms = "2022-04-14_100122_0_3.ms"

    components = raw_ms_format(ms)
    assert isinstance(components, RawNameComponents)
    assert components.beam == "0"
    assert components.date == "2022-04-14"
    assert components.time == "100122"
    assert components.spw == "3"


def test_components_all_beams_spws():
    for beam in range(36):
        for spw in range(10):
            ms = f"2022-04-14_100122_{beam}_{spw}.ms"

            components = raw_ms_format(ms)
            assert isinstance(components, RawNameComponents)
            assert components.beam == f"{beam}"
            assert components.date == "2022-04-14"
            assert components.time == "100122"
            assert components.spw == f"{spw}"

    for beam in range(36):
        ms = f"2022-04-14_100122_{beam}.ms"

        components = raw_ms_format(ms)
        assert isinstance(components, RawNameComponents)
        assert components.beam == f"{beam}"
        assert components.date == "2022-04-14"
        assert components.time == "100122"
        assert components.spw is None


def test_create_ms_name():
    example_path = "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0.ms"
    expected = "SB39400.beam00.ms"
    ms_path = create_ms_name(ms_path=Path(example_path))
    assert isinstance(ms_path, str)
    assert ms_path == expected
    assert ms_path.endswith(".ms")

    example_path_2 = "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0.ms"
    expected_2 = "SB39400.RACS_0635-31.beam00.ms"
    ms_path_2 = create_ms_name(ms_path=Path(example_path_2), field="RACS_0635-31")
    assert isinstance(ms_path_2, str)
    assert ms_path_2 == expected_2

    example_path_3 = "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0.ms"
    expected_3 = "SB1234.RACS_0635-31.beam00.ms"
    ms_path_3 = create_ms_name(
        ms_path=Path(example_path_3), sbid=1234, field="RACS_0635-31"
    )
    assert isinstance(ms_path_3, str)
    assert ms_path_3 == expected_3

    example_path_4 = "/scratch3/gal16b/askap_sbids/39400/2022-04-14_100122_0_12.ms"
    expected_4 = "SB1234.RACS_0635-31.beam00.spw12.ms"
    ms_path_4 = create_ms_name(
        ms_path=Path(example_path_4), sbid=1234, field="RACS_0635-31"
    )
    assert isinstance(ms_path_4, str)
    assert ms_path_4 == expected_4

    examples = [
        "scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms",
    ]
    for ex in examples:
        name = create_ms_name(ms_path=Path(ex))
        assert name == "SB40470.RACS_1237+00.beam35.ms"


def test_create_ms_name_no_sbid():
    example_path = "2022-04-14_100122_0.ms"
    expected = "SB0000.beam00.ms"
    ms_path = create_ms_name(ms_path=example_path)
    assert isinstance(ms_path, str)
    assert ms_path == expected

    expected_2 = "SB0000.RACS_0635-31.beam00.ms"
    ms_path_2 = create_ms_name(ms_path=example_path, field="RACS_0635-31")
    assert isinstance(ms_path_2, str)
    assert ms_path_2 == expected_2

    example_path_3 = "2022-04-14_100122_0_12.ms"
    expected_3 = "SB0000.RACS_0635-31.beam00.spw12.ms"
    ms_path_3 = create_ms_name(ms_path=example_path_3, field="RACS_0635-31")
    assert isinstance(ms_path_3, str)
    assert ms_path_3 == expected_3


def test_formatted_name_components_withpol():
    """Tests around the pol field in a file name"""
    ex = "SB39400.RACS_0635-31.beam33-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round is None
    assert components.pol is None

    ex = "SB39400.RACS_0635-31.beam33.i-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round is None
    assert components.pol == "i"

    ex = "SB39400.RACS_0635-31.beam33.round2.i-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "2"
    assert components.pol == "i"

    ex = "SB39400.RACS_0635-31.beam33.round2.iq-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "2"
    assert components.pol == "iq"

    ex = "SB39400.RACS_0635-31.beam33.round2-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "2"
    assert components.pol is None


def test_formatted_name_components():
    ex = "SB39400.RACS_0635-31.beam33-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round is None

    ex = "SB39400.RACS_0635-31.beam33.spw123-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw == "123"
    assert components.round is None

    ex_path = Path(
        "/this/is/and/example/path/SB39400.RACS_0635-31.beam33.spw123-MFS-image.conv.fits"
    )

    components = processed_ms_format(in_name=ex_path)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw == "123"
    assert components.round is None


def test_formatted_name_components_wchannelrange():
    ex = "SB39400.RACS_0635-31.beam33.round1.i.ch0100-1009.MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "1"
    assert components.pol == "i"
    assert components.channel_range == (100, 1009)

    ex = "SB39400.RACS_0635-31.beam33.round1.ch0100-1009.MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "1"
    assert components.pol is None
    assert components.channel_range == (100, 1009)


def test_formatted_name_components_wround():
    ex = "SB39400.RACS_0635-31.beam33.round1-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw is None
    assert components.round == "1"

    ex = "SB39400.RACS_0635-31.beam33.spw123.round12-MFS-image.conv.fits"

    components = processed_ms_format(in_name=ex)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw == "123"
    assert components.round == "12"

    ex_path = Path(
        "/this/is/and/example/path/SB39400.RACS_0635-31.beam33.spw123.round123-MFS-image.conv.fits"
    )

    components = processed_ms_format(in_name=ex_path)
    assert isinstance(components, ProcessedNameComponents)
    assert components.sbid == "39400"
    assert components.field == "RACS_0635-31"
    assert components.beam == "33"
    assert components.spw == "123"
    assert components.round == "123"


def test_get_correct_name_format():
    examples = [
        "SB39400.RACS_0635-31.beam33-MFS-image.conv.fits",
        "SB39400.RACS_0635-31.beam33.ms",
        Path(
            "/this/is/and/example/path/SB39400.RACS_0635-31.beam33.spw123-MFS-image.conv.fits"
        ),
    ]

    for ex in examples:
        res = extract_components_from_name(name=ex)
        assert isinstance(res, ProcessedNameComponents)

    examples = ["2022-04-14_100122_0.averaged.ms"]

    for ex in examples:
        res = extract_components_from_name(name=ex)
        assert isinstance(res, RawNameComponents)

    examples = [
        "scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms",
        Path(
            "scienceData.RACS_1237+00.SB40470.RACS_1237+00.beam35_averaged_cal.leakage.ms"
        ),
    ]
    for ex in examples:
        res = extract_components_from_name(name=ex)
        assert isinstance(res, CASDANameComponents)


def test_get_beam_from_name():
    assert extract_beam_from_name(name="2022-04-14_100122_0.averaged.ms") == 0
    assert (
        extract_beam_from_name(name="SB39400.RACS_0635-31.beam33-MFS-image.conv.fits")
        == 33
    )


def get_lots_of_names() -> list[Path]:
    examples = [
        "59058/SB59058.RACS_1626-84.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0070-0071.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0142-0143.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0214-0215.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0286-0287.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0071-0072.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0143-0144.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0215-0216.linmos.fits",
        "59058/SB59058.RACS_1626-84.ch0287-0288.linmos.fits",
    ]

    return list(map(Path, examples))


def test_create_name_from_common_fields():
    """See if we can identify the common bits of names as recognised in the process name format"""
    examples = get_lots_of_names()

    common_names = create_name_from_common_fields(in_paths=examples)
    expected_common_name = Path("59058/SB59058.RACS_1626-84")

    assert common_names == expected_common_name

    for additional_suffix in (".linmos.fits", "linmos.fits"):
        common_names = create_name_from_common_fields(
            in_paths=examples, additional_suffixes=additional_suffix
        )
        expected_common_name = Path("59058/SB59058.RACS_1626-84.linmos.fits")

        assert common_names == expected_common_name

    examples.append("This/will/raise/a/valuerror")

    with pytest.raises(ValueError):
        create_name_from_common_fields(in_paths=examples)


def get_lots_of_names_2() -> list[Path]:
    examples = [
        "59058/SB59058.RACS_1626-84.round4.i.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0285-0286.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0070-0071.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0142-0143.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0214-0215.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0286-0287.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0071-0072.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0143-0144.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0215-0216.linmos.fits",
        "59058/SB59058.RACS_1626-84.round4.i.ch0287-0288.linmos.fits",
    ]

    return list(map(Path, examples))


def test_create_name_from_common_fields_2():
    """See if we can identify the common bits of names as recognised in the process name format.
    This picks up some missing formats that this sea dog initially overlookede"""
    examples = get_lots_of_names_2()

    common_names = create_name_from_common_fields(in_paths=examples)
    expected_common_name = Path("59058/SB59058.RACS_1626-84.round4.i")

    assert common_names == expected_common_name

    for additional_suffix in (".linmos.fits", "linmos.fits"):
        common_names = create_name_from_common_fields(
            in_paths=examples, additional_suffixes=additional_suffix
        )
        expected_common_name = Path("59058/SB59058.RACS_1626-84.round4.i.linmos.fits")

        assert common_names == expected_common_name

    examples.append("This/will/raise/a/valuerror")

    with pytest.raises(ValueError):
        create_name_from_common_fields(in_paths=examples)


def get_lots_of_names_3():
    files = [
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam00.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam01.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam02.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam03.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam04.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam05.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam06.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam07.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
        PosixPath(
            "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.beam08.round4.i.ch0238-0239.image.optimal.conv.fits"
        ),
    ]

    return files


def test_create_name_from_common_fields_3():
    """Test a strange combination of errors around the common field name"""

    files = get_lots_of_names_3()

    common_name = create_name_from_common_fields(in_paths=files)
    assert common_name == PosixPath(
        "/scratch3/gal16b/spectral_flow/57516/SB57516.RACS_0929-81.round4.i.ch0238-0239"
    )


def test_create_linmos_parset_base_path():
    """The yandasoft linmos task writes out a configuration file.
    This function tests the generation of the path"""
    examples = get_lots_of_names_2()

    expected = Path("59058/SB59058.RACS_1626-84.round4.i").absolute()
    assert expected == create_linmos_base_path(input_images=examples)

    expected = Path("59058/SB59058.RACS_1626-84.round4.i.jack.sparrow").absolute()
    assert expected == create_linmos_base_path(
        input_images=examples, additional_suffixes="jack.sparrow"
    )
    new_paths = [Path("/Here/Be/Pirates") / p for p in examples]
    expected = Path("/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i").absolute()
    assert expected == create_linmos_base_path(input_images=new_paths)

    expected = Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.jack.sparrow"
    ).absolute()
    assert expected == create_linmos_base_path(
        input_images=new_paths, additional_suffixes="jack.sparrow"
    )


def test_create_linmos_names():
    """Some logic is around creating names to give to linmos to create the output images, weights and parset"""

    linmos_names = create_linmos_names(
        name_prefix="/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i"
    )
    assert linmos_names.image_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.linmos.fits"
    )
    assert linmos_names.weight_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.weight.fits"
    )
    assert linmos_names.parset_output_path == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i_parset.txt"
    )

    linmos_names = create_linmos_names(
        name_prefix=Path("/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i")
    )
    assert linmos_names.image_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.linmos.fits"
    )
    assert linmos_names.weight_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.weight.fits"
    )
    assert linmos_names.parset_output_path == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i_parset.txt"
    )

    linmos_names = create_linmos_names(
        name_prefix="/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i",
        parset_output_path=Path("Jack/My/Boi.txt"),
    )
    assert linmos_names.image_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.linmos.fits"
    )
    assert linmos_names.weight_fits == Path(
        "/Here/Be/Pirates/59058/SB59058.RACS_1626-84.round4.i.weight.fits"
    )
    assert linmos_names.parset_output_path == Path("Jack/My/Boi.txt")


def test_rename_linear_to_stokes():
    linear_names = (
        "jack.qu.sparrow",
        "qu.sparrow.jack",
        ".qu.sparrow.jack",
        "sparrow.jack.qu",
    )
    stokes_q_names = (
        "jack.q.sparrow",
        "qu.sparrow.jack",
        ".q.sparrow.jack",
        "sparrow.jack.q",
    )
    stokes_u_names = (
        "jack.u.sparrow",
        "qu.sparrow.jack",
        ".u.sparrow.jack",
        "sparrow.jack.u",
    )

    for stokes, stokes_names in zip(("q", "u"), (stokes_q_names, stokes_u_names)):
        for linear_name, check_name in zip(linear_names, stokes_names):
            stokes_name = _rename_linear_to_stokes(linear_name, stokes)
            assert stokes_name == check_name

    with pytest.raises(NameError):
        _rename_linear_to_stokes("jack.sparrow", "i")
        _rename_linear_to_stokes("jack.sparrow", "v")
        _rename_linear_to_stokes("jack.sparrow", "pearl")

    stokes_path = rename_linear_to_stokes(
        linear_name=Path("jack.qu.sparrow"), stokes="q"
    )
    assert isinstance(stokes_path, Path)
    stokes_path = rename_linear_to_stokes(linear_name="jack.qu.sparrow", stokes="q")
    assert isinstance(stokes_path, str)


def test_split_images():
    with pytest.raises(ValueError):
        split_images(images=[Path("jack.sparrow.i.fits")], by="pol")

    pols = "iquv"
    images = [Path(f"SB1234.FieldName.beam00.round4.{p}.fits") for p in pols]

    split_dict = split_images(images=images, by="pol")

    assert len(split_dict) == 4
    assert split_dict.keys() == set(pols)
    for key, val in split_dict.items():
        assert len(val) == 1
        assert val[0] == Path(f"SB1234.FieldName.beam00.round4.{key}.fits")

    images = [
        Path("SB1234.FieldName.beam00.round4.i.fits"),
        Path("SB1234.FieldName.beam00.round4.i.MFS.fits"),
        Path("SB1234.FieldName.beam00.round4.v.fits"),
    ]

    split_dict = split_images(images=images, by="pol")
    assert len(split_dict) == 2
    assert split_dict.keys() == {"i", "v"}
    assert len(split_dict["i"]) == 2
    assert len(split_dict["v"]) == 1

    sbids = [
        "1234",
        "5678",
        "9012",
    ]

    images = [Path(f"SB{sbid}.FieldName.beam00.round4.i.fits") for sbid in sbids]
    split_dict = split_images(images=images, by="sbid")
    assert len(split_dict) == 3
    assert list(split_dict.keys()) == list(sbids)
    for key, val in split_dict.items():
        assert len(val) == 1
        assert val[0] == Path(f"SB{key}.FieldName.beam00.round4.i.fits")

    with pytest.raises(NamingException):
        split_images(images=images, by="jack")
