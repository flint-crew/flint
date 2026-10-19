"""Testing some wsclean functionality."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
)
from flint.imager.wsclean import (
    WSCleanOptions,
    WSCleanResult,
    _rename_wsclean_file,
    _rename_wsclean_title,
    _resolve_wsclean_key_value_to_cli_str,
    _wsclean_output_callback,
    create_wsclean_cmd,
    create_wsclean_name_argument,
    get_parser,
    get_wsclean_output_names,
    get_wsclean_output_source_list_path,
    rename_wsclean_prefix_in_image_set,
)
from flint.imageset import (
    ImageSet,
)
from flint.naming import create_imaging_name_prefix
from flint.options import MS


def test_get_cli_parser() -> None:
    """capn_crunch was throwing error over duplicated options being added to
    the argpase object. No conflicting options means the parser should just
    be returned"""
    _ = get_parser()


def test_get_wsclean_output_source_list_path():
    """Wsclean can be configured out output a source list of the
    components, their brightness and relative size that were placed
    throughout cleaning. Here we be testing whether we can
    generate the expected name"""

    example = Path("/flint/pirates/SB58992.RACS_1726-73.beam22.ms")
    source_path = Path("/flint/pirates/SB58992.RACS_1726-73.beam22.i-sources.txt")

    test_source_path = get_wsclean_output_source_list_path(name_path=example, pol="i")
    assert source_path == test_source_path

    example = Path("/flint/pirates/SB58992.RACS_1726-73.beam22")
    source_path = Path("/flint/pirates/SB58992.RACS_1726-73.beam22.i-sources.txt")

    test_source_path = get_wsclean_output_source_list_path(name_path=example, pol="i")
    assert source_path == test_source_path

    example = "SB58992.RACS_1726-73.beam22"
    source_path = Path("SB58992.RACS_1726-73.beam22.i-sources.txt")

    test_source_path = get_wsclean_output_source_list_path(name_path=example, pol="i")
    assert source_path == test_source_path

    example = "SB58992.RACS_1726-73.beam22"
    source_path = Path("SB58992.RACS_1726-73.beam22-sources.txt")

    test_source_path = get_wsclean_output_source_list_path(name_path=example, pol=None)
    assert source_path == test_source_path


@pytest.fixture(scope="session", autouse=True)
def set_env():
    """Set up variables for a specific test"""
    os.environ["LOCALDIR"] = "Pirates/be/here"


def test_rename_wsclean_path_move(tmpdir: Any):
    """Rename the wsclean supplied part of a filename while moving a file"""
    test_path = Path(tmpdir) / "move_file/"
    test_path.mkdir(parents=True, exist_ok=True)

    ex = test_path / Path("SB39400.RACS_0635-31.beam33.poli-MFS-image.fits")
    out_ex = test_path / Path("SB39400.RACS_0635-31.beam33.poli.MFS.image.fits")

    with open(ex, "w") as out_file:
        out_file.write("example")

    assert ex.exists()
    assert not out_ex.exists()
    assert _rename_wsclean_file(input_path=ex, rename_file=True) == out_ex
    assert not ex.exists()
    assert out_ex.exists()


def _write_test_image(items: Any):
    for item in items:
        with Path(item).open("w") as out_file:
            out_file.write(str(item))


def test_rename_wsclean_image_set(tmpdir: Any):
    """Ensure that items described in an image set are able to be properly renamed"""

    test_dir = Path(tmpdir) / "image_setrename"
    test_dir.mkdir(parents=True, exist_ok=True)

    # create some test files and ensure they all exist
    keys: dict[Any, Any] = {}
    prefix = f"{test_dir!s}/SB39400.RACS_0635-31.beam33.i"
    keys["prefix"] = prefix
    for mode in ("image", "residual"):
        items = [
            Path(f"{prefix}-{subband:04d}-{mode}.fits") for subband in range(4)
        ] + [Path(f"{prefix}-MFS-{mode}.fits")]
        _write_test_image(items=items)
        keys[mode] = items
        assert all([Path(f).exists() for f in items])

    # form the image set that will have the wsclean appended properties string renamed
    image_set = ImageSet(**keys)
    assert isinstance(image_set, ImageSet)
    new_image_set = rename_wsclean_prefix_in_image_set(input_image_set=image_set)

    # test to see thhat files exists
    assert new_image_set.prefix == prefix
    assert new_image_set.image is not None
    assert all([file.exists() for file in new_image_set.image])
    assert new_image_set.residual is not None
    assert all([file.exists() for file in new_image_set.residual])

    # and ensure the originals no longer exist
    assert all([not Path(file).exists() for file in keys["image"]])
    assert all([not (file).exists() for file in keys["residual"]])


def test_rename_wsclean_path():
    """Rename the wsclean supplied part of a filename"""

    ex = Path("SB39400.RACS_0635-31.beam33.poli-MFS-image.fits")
    out_ex = Path("SB39400.RACS_0635-31.beam33.poli.MFS.image.fits")
    assert _rename_wsclean_file(input_path=ex) == out_ex

    ex = Path("SB39400.RACS_0635-31.beam33.poli-MFS-image")
    out_ex = Path("SB39400.RACS_0635-31.beam33.poli.MFS.image")
    assert _rename_wsclean_file(input_path=ex) == out_ex

    ex = Path("/a/path/that/is/a/parent/SB39400.RACS_0635-31.beam33.poli-MFS-image")
    out_ex = Path("/a/path/that/is/a/parent/SB39400.RACS_0635-31.beam33.poli.MFS.image")
    assert _rename_wsclean_file(input_path=ex) == out_ex

    # This one makes sure that the second component of ch0234-0345 is not pixked up
    # as the wsclean added subband indicator
    ex = Path("/a/path/that/is/a/parent/SB39400.RACS_0635-31.beam33.poli-MFS-image")
    out_ex = Path("/a/path/that/is/a/parent/SB39400.RACS_0635-31.beam33.poli.MFS.image")
    assert _rename_wsclean_file(input_path=ex) == out_ex

    ex = Path(
        "/a/path/that/is/a/parent/SB57516.RACS_0929-81.beam35.round4.i.ch0287-0288-image.fits"
    )
    out_ex = Path(
        "/a/path/that/is/a/parent/SB57516.RACS_0929-81.beam35.round4.i.ch0287-0288.image.fits"
    )
    assert _rename_wsclean_file(input_path=ex) == out_ex


def test_rename_stokes_v_model():
    """Some model files are not being renamed correctly. Arr"""

    ex = "SB57988.RACS_1415-46.beam34.round4.v-MFS-model.fits"
    out_ex = "SB57988.RACS_1415-46.beam34.round4.v.MFS.model.fits"

    assert _rename_wsclean_title(name_str=ex) == out_ex


def test_regex_rename_wsclean_title():
    """Rename the wsclean supplied using regex"""

    ex = "SB39400.RACS_0635-31.beam33.poli-MFS-image.fits"
    out_ex = "SB39400.RACS_0635-31.beam33.poli.MFS.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB39400.RACS_0635-31.beam33.poli-MFS-image"
    out_ex = "SB39400.RACS_0635-31.beam33.poli.MFS.image"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB39400.RACS_0635-31.beam33.poli-MFS-image"
    out_ex = "SB39400.RACS_0635.31.beam33.poli.MFS.image"
    assert not _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB39400.RACS_0635-31.beam33.poli.MFS.image.fits"
    out_ex = "SB39400.RACS_0635-31.beam33.poli.MFS.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex
    assert _rename_wsclean_title(name_str=ex) is ex

    ex = "SB39400.RACS_0635-31.beam33.poli-i-MFS-image"
    out_ex = "SB39400.RACS_0635-31.beam33.poli.i.MFS.image"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB39400.RACS_0635-31.beam33.ch109-110-i-MFS-image"
    out_ex = "SB39400.RACS_0635-31.beam33.ch109-110.i.MFS.image"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB39400.RACS_0635-31.beam33.i.ch109-110-i-MFS-image"
    out_ex = "SB39400.RACS_0635-31.beam33.i.ch109-110.i.MFS.image"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    # wsclean's own bare per-channel index (no flint ch<lo>-<hi> range) must still
    # be dot-converted rather than left as a stray hyphen - regression test for the
    # negative lookbehind typo that previously left this as `i-0000-image`. It is
    # also reformatted into flint's ch<lo>-<hi> range so it parses back out as a
    # proper channel_range rather than an ambiguous bare index.
    ex = "SB56289.RACS_1041+18.beam00.round1.i-0000-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam00.round1.i.ch0000-0001.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex


def test_regex_rename_wsclean_title_timestep():
    """wsclean's own `-t00005` timestep suffix (from interval-based imaging) must
    be folded into flint's scan<lo>-<hi> range convention, not left as a bare
    index or mislabelled as a multiscale term."""
    ex = "SB56289.RACS_1041+18.beam00.round1.i-0000-t00005-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam00.round1.i.ch0000-0001.scan0005-0006.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex


def test_regex_stokes_wsclean_title():
    """Test whether all stokes values are picked up properly"""

    prefix = "SB39400.RACS_0635-31.beam33.poli."
    end = "-MFS-image.fits"
    transformed = end.replace("-", ".")

    for stokes in ("i", "q", "u", "v", "xx", "xy", "yx", "yy"):
        ex = f"{prefix}-{stokes}{end}"
        out_ex = f"{prefix}.{stokes}{transformed}"
        assert _rename_wsclean_title(name_str=ex) == out_ex

    name = "SB59058.RACS_1626-84.beam34.round4.i.ch287-288-image.fits"
    out_name = "SB59058.RACS_1626-84.beam34.round4.i.ch287-288.image.fits"
    assert _rename_wsclean_title(name_str=name) == out_name


@pytest.mark.parametrize(
    "project",
    [
        "pol",
        "image",
        "dirty",
        "model",
        "residual",
        "psf",
        "MFS",
        "i",
        "q",
        "jack",
        "sparrow",
    ],
)
def test_rename_wsclean_title_project_field_collision(project: str):
    """The `project` field is a free-form token (e.g. `.project-<name>.`) that sits
    ahead of the wsclean-appended suffix in the file name. If a project name happens
    to collide with a reserved wsclean token (an image type, MFS, or a pol letter),
    the wsclean suffix search must not match inside the project token instead of the
    real trailing suffix."""
    ex = f"SB56289.RACS_1041+18.project-{project}.beam15.round1.i-0000-image.fits"
    out_ex = (
        f"SB56289.RACS_1041+18.project-{project}.beam15.round1.i.ch0000-0001.image.fits"
    )
    assert _rename_wsclean_title(name_str=ex) == out_ex


@pytest.mark.parametrize("suffix", ["racs_all", "pol-v2", "v2.1"])
def test_flint_name_suffix_must_parse_back(suffix: str):
    """The suffix is written into names as `.project-<suffix>.`, which the name
    parser only reads back when it is letters and digits. Anything else would
    silently cost every field after it, so it is refused up front."""
    with pytest.raises(ValidationError):
        WSCleanOptions(flint_name_suffix=suffix)
    with pytest.raises(ValidationError):
        WSCleanOptions().with_options(flint_name_suffix=suffix)


def test_flint_name_suffix_accepts_letters_and_digits():
    assert WSCleanOptions(flint_name_suffix="pol2").flint_name_suffix == "pol2"
    assert WSCleanOptions().flint_name_suffix is None


def test_rename_wsclean_title_qu_joint_pol():
    """The join_polarizations 'qu' tag should be folded into a single-letter
    pol field per wsclean's own per-image -Q/-U suffix, for both per-channel
    and MFS outputs."""
    ex = "SB56289.RACS_1041+18.beam15.round1.qu-0000-Q-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam15.round1.q.ch0000-0001.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB56289.RACS_1041+18.beam15.round1.qu-0000-U-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam15.round1.u.ch0000-0001.image.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex

    ex = "SB56289.RACS_1041+18.beam15.round1.qu-MFS-Q-residual.fits"
    out_ex = "SB56289.RACS_1041+18.beam15.round1.q.MFS.residual.fits"
    assert _rename_wsclean_title(name_str=ex) == out_ex


def test_resolve_key_value_to_cli():
    """The wsclean command generation operates over keys and values, and
    the formatting is partly based on the type a value has. This goes through
    those checks"""
    res = _resolve_wsclean_key_value_to_cli_str("size", 1024)
    assert res.cmd == "-size 1024 1024"
    assert res.bindpath is None
    assert res.unknown is None

    res = _resolve_wsclean_key_value_to_cli_str("no_update_model_required", True)
    assert res.cmd == "-no-update-model-required"
    assert res.bindpath is None
    assert res.unknown is None

    res = _resolve_wsclean_key_value_to_cli_str("no_update_model_required", False)
    assert res.cmd is None
    assert res.bindpath is None
    assert res.unknown is None

    res = _resolve_wsclean_key_value_to_cli_str("temp_dir", Path("jack/sparrow"))
    assert res.cmd == "-temp-dir jack/sparrow"
    assert res.bindpath == Path("jack/sparrow")
    assert res.unknown is None

    unknown = WSCleanOptions
    res = _resolve_wsclean_key_value_to_cli_str("temp_dir", unknown)
    assert res.cmd is None
    assert res.bindpath is None
    assert res.unknown == ("temp_dir", unknown)

    ignore = WSCleanOptions
    res = _resolve_wsclean_key_value_to_cli_str("flint_this_should_be_ignored", ignore)
    assert res.cmd is None
    assert res.bindpath is None
    assert res.unknown is None
    assert res.ignore


def test_create_wsclean_name(ms_example) -> None:
    """Test the creation of a wsclean name argument"""
    name = create_imaging_name_prefix(ms_path=ms_example)
    assert name == "SB39400.RACS_0635-31.beam0.small"

    for pol in ("i", "I"):
        name = create_imaging_name_prefix(ms_path=ms_example, pol=pol)
        assert name == "SB39400.RACS_0635-31.beam0.small.i"


def test_create_wsclean_name_argument(ms_example):
    """Ensure that the generated name argument behaves as expected"""

    ms = MS.cast(ms=Path(ms_example))
    wsclean_options = WSCleanOptions()
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options, ms=ms
    )

    parent = str(Path(ms_example).parent)
    assert isinstance(name_argument_path, Path)
    assert f"{parent}/SB39400.RACS_0635-31.beam0.small.i" == str(name_argument_path)

    wsclean_options_2 = WSCleanOptions(temp_dir="/jack/sparrow")
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options_2, ms=ms
    )

    assert "/jack/sparrow/SB39400.RACS_0635-31.beam0.small.i" == str(name_argument_path)

    wsclean_options_3 = WSCleanOptions(flint_name_suffix="pol")
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options_3, ms=ms
    )

    assert f"{parent}/SB39400.RACS_0635-31.project-pol.beam00.i" == str(
        name_argument_path
    )


def test_create_wsclean_name_argument_hold_dir(ms_example) -> None:
    """flint_hold_dir sets the -name directory, and takes precedence over temp_dir"""

    ms = MS.cast(ms=Path(ms_example))
    name = "SB39400.RACS_0635-31.beam0.small.i"

    wsclean_options = WSCleanOptions(flint_hold_dir="/jack/sparrow")
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options, ms=ms
    )

    assert f"/jack/sparrow/{name}" == str(name_argument_path)

    wsclean_options_2 = WSCleanOptions(
        flint_hold_dir="/jack/sparrow", temp_dir="/davey/jones"
    )
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options_2, ms=ms
    )

    assert f"/jack/sparrow/{name}" == str(name_argument_path)


def test_create_wsclean_name_argument_with_list_mss(ms_example) -> None:
    """Ensure that the generated name argument behaves as expected.
    This uses list of MS to create the base name."""

    ms = [
        MS.cast(ms)
        for ms in (
            Path(ms_example),
            Path("SB39400.RACS_0635-31.beam0"),
            Path("SB39401.RACS_0635-31.beam0"),
            Path("SB39402.RACS_0635-31.beam0"),
        )
    ]

    wsclean_options = WSCleanOptions()
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options, ms=ms
    )

    parent = str(Path(ms_example).parent)
    assert isinstance(name_argument_path, Path)
    assert f"{parent}/SB39400.RACS_0635-31.beam0.small.i" == str(name_argument_path)

    wsclean_options_2 = WSCleanOptions(temp_dir="/jack/sparrow")
    name_argument_path = create_wsclean_name_argument(
        wsclean_options=wsclean_options_2, ms=ms
    )

    assert "/jack/sparrow/SB39400.RACS_0635-31.beam0.small.i" == str(name_argument_path)


def test_create_wsclean_command(ms_example):
    """Test whether WSCleanOptions can be correctly cast to a command string"""
    wsclean_options = WSCleanOptions()

    command = create_wsclean_cmd(
        ms_list=MS.cast(ms_example), wsclean_options=wsclean_options
    )
    assert isinstance(command, WSCleanResult)


def test_create_wsclean_command_excludes_flint_save_mfs_products(ms_example):
    """flint_save_mfs_products is a flint-only field (like flint_name_suffix)
    and should not leak into the generated wsclean command"""
    wsclean_options = WSCleanOptions(flint_save_mfs_products=True)

    command = create_wsclean_cmd(
        ms_list=MS.cast(ms_example), wsclean_options=wsclean_options
    )
    assert isinstance(command, WSCleanResult)
    assert "flint" not in command.cmd
    assert "mfs" not in command.cmd.lower()


def test_create_wsclean_command_hold_dir(ms_example, tmpdir) -> None:
    """flint_hold_dir drives -name only, and leaves -temp-dir alone"""
    hold_dir = Path(tmpdir) / "hold"
    temp_dir = Path(tmpdir) / "temp"
    wsclean_options = WSCleanOptions(flint_hold_dir=hold_dir, temp_dir=temp_dir)

    command = create_wsclean_cmd(
        ms_list=MS.cast(ms_example), wsclean_options=wsclean_options
    )

    assert "flint" not in command.cmd
    assert f"-temp-dir {temp_dir}" in command.cmd
    assert f"-name {hold_dir}/" in command.cmd
    assert hold_dir in command.bind_dirs
    assert command.move_hold_directories == (Path(ms_example).parent, hold_dir)


def test_create_wsclean_command_with_list_ms(ms_example) -> None:
    """Test whether WSCleanOptions can be correctly cast to a command string
    when using a list of MS instance"""
    wsclean_options = WSCleanOptions()

    mss = [
        MS.cast(ms)
        for ms in (
            Path(ms_example),
            Path("SB1234.JACK_0001+234.beam00"),
        )
    ]
    assert isinstance(mss, list)
    assert all([isinstance(_ms, MS) for _ms in mss])

    command = create_wsclean_cmd(ms_list=mss, wsclean_options=wsclean_options)
    assert isinstance(command, WSCleanResult)

    for _ms in mss:
        assert _ms.path.name in command.cmd


def test_create_wsclean_command_with_environment(ms_example):
    """Test whether WSCleanOptions can be correctly cast to a command string"""
    wsclean_options = WSCleanOptions(temp_dir="$LOCALDIR")

    command = create_wsclean_cmd(
        ms_list=[
            MS.cast(ms_example),
        ],
        wsclean_options=wsclean_options,
    )
    assert isinstance(command, WSCleanResult)
    assert "Pirates/be/here" in command.cmd
    assert command.cmd.startswith("wsclean ")


def test_wsclean_divergence():
    """Make sure the wsclean call back function picks up divergence and raises appropriate errors"""
    good = (
        "Iteration 59228, scale 0 px : -862.94 µJy at 3729,3746",
        "Opening reordered part 0 spw 0 for /scratch3/gal16b/flint_peel/40470/SB40470.RACS_1237+00.beam4.round1.ms",
        "Opening reordered part 0 spw 0 for /scratch3/gal16b/flint_peel/40470/SB40470.RACS_1237+00.beam4.round1.ms",
        "Although KJy there is no iterate ion, not the lack of a capital-I and the space, clever pirate",
    )
    for g in good:
        _wsclean_output_callback(line=g)

    bad = "Iteration 59228, scale 0 px : -862.94 KJy at 3729,3746"
    with pytest.raises(CleanDivergenceError):
        _wsclean_output_callback(line=bad)

    with pytest.raises(AssertionError):
        _wsclean_output_callback(line=tuple("A tuple of text".split()))


def test_attemptrerun_wsclean_output_callback():
    """Some known lines output by wsclean can be caused by some transient
    type of error. In such a situation AttemptRerunException should
    be raised."""

    good = (
        "Iteration 59228, scale 0 px : -862.94 µJy at 3729,3746",
        "Opening reordered part 0 spw 0 for /scratch3/gal16b/flint_peel/40470/SB40470.RACS_1237+00.beam4.round1.ms",
        "Opening reordered part 0 spw 0 for /scratch3/gal16b/flint_peel/40470/SB40470.RACS_1237+00.beam4.round1.ms",
        "Although Input/output is here, it is not next to error",
        "Similar with temporary data file error opening error",
    )
    for g in good:
        _wsclean_output_callback(line=g)

    bad = (
        "Input/output error",
        "But why is the rum gone... Input/output error",
        "Input/output error should cause a remake of Pirates of the Caribbean",
    )
    for b in bad:
        with pytest.raises(AttemptRerunException):
            _wsclean_output_callback(line=b)


def test_wsclean_output_named_raises():
    with pytest.raises(FileExistsError):
        _ = get_wsclean_output_names(
            prefix="JackSparrow", subbands=4, verify_exists=True
        )


def test_wsclean_output_named_check_when_adding():
    image_set = get_wsclean_output_names(
        prefix="JackSparrow",
        subbands=4,
        verify_exists=True,
        check_exists_when_adding=True,
    )

    assert isinstance(image_set, ImageSet)
    assert len(image_set.image) == 0


def test_wsclean_output_named():
    image_set = get_wsclean_output_names(prefix="JackSparrow", subbands=4)

    assert isinstance(image_set, ImageSet)
    assert image_set.prefix == "JackSparrow"

    assert image_set.image is not None
    assert len(image_set.image) == 5
    assert isinstance(image_set.image[0], Path)

    assert image_set.dirty is not None
    assert len(image_set.dirty) == 5
    assert isinstance(image_set.dirty[0], Path)

    assert image_set.model is not None
    assert len(image_set.model) == 5
    assert isinstance(image_set.model[0], Path)

    assert image_set.residual is not None
    assert len(image_set.residual) == 5
    assert isinstance(image_set.residual[0], Path)

    assert image_set.psf is not None
    assert len(image_set.psf) == 5
    assert isinstance(image_set.psf[0], Path)


def test_wsclean_output_named_wpols():
    image_set = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, pols=("I", "Q")
    )

    assert isinstance(image_set, ImageSet)
    assert image_set.prefix == "JackSparrow"

    expected = 10
    assert image_set.image is not None
    assert len(image_set.image) == expected
    assert isinstance(image_set.image[0], Path)

    assert image_set.dirty is not None
    assert len(image_set.dirty) == expected
    assert isinstance(image_set.dirty[0], Path)

    assert image_set.model is not None
    assert len(image_set.model) == expected
    assert isinstance(image_set.model[0], Path)

    assert image_set.residual is not None
    assert len(image_set.residual) == expected
    assert isinstance(image_set.residual[0], Path)

    assert image_set.psf is not None
    assert len(image_set.psf) == 5  # PSF is the same across all pols
    assert isinstance(image_set.psf[0], Path)

    assert image_set.image[0] == Path("JackSparrow-0000-I-image.fits")
    assert image_set.image[4] == Path("JackSparrow-MFS-I-image.fits")
    assert image_set.image[5] == Path("JackSparrow-0000-Q-image.fits")
    assert image_set.image[9] == Path("JackSparrow-MFS-Q-image.fits")


def test_wsclean_output_named_nomfs():
    image_set = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, include_mfs=False
    )

    assert isinstance(image_set, ImageSet)
    assert image_set.prefix == "JackSparrow"

    assert image_set.image is not None
    assert len(image_set.image) == 4
    assert isinstance(image_set.image[0], Path)

    assert image_set.dirty is not None
    assert len(image_set.dirty) == 4
    assert isinstance(image_set.dirty[0], Path)

    assert image_set.model is not None
    assert len(image_set.model) == 4
    assert isinstance(image_set.model[0], Path)

    assert image_set.residual is not None
    assert len(image_set.residual) == 4
    assert isinstance(image_set.residual[0], Path)

    assert image_set.psf is not None
    assert len(image_set.psf) == 4
    assert isinstance(image_set.psf[0], Path)


def test_wsclean_names_no_subbands():
    """The spectral line modes image per channel, so there is therefore no subband type
    in the wsclean named output"""
    image_set = get_wsclean_output_names(
        prefix="JackSparrow", subbands=1, include_mfs=False
    )

    assert isinstance(image_set, ImageSet)
    assert image_set.prefix == "JackSparrow"

    assert image_set.image
    assert len(image_set.image) == 1
    assert image_set.image[0] == Path("JackSparrow-image.fits")

    assert image_set.psf
    assert len(image_set.psf) == 1
    assert image_set.psf[0] == Path("JackSparrow-psf.fits")
