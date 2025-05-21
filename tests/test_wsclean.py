"""Testing some wsclean functionality."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
    NamingException,
)
from flint.imager.wsclean import (
    ImageSet,
    WSCleanOptions,
    WSCleanResult,
    _rename_wsclean_file,
    _rename_wsclean_title,
    _resolve_wsclean_key_value_to_cli_str,
    _wsclean_output_callback,
    combine_image_set_to_cube,
    create_wsclean_cmd,
    create_wsclean_name_argument,
    get_wsclean_output_names,
    get_wsclean_output_source_list_path,
    merge_image_sets,
    rename_wsclean_prefix_in_image_set,
    split_and_get_image_set,
    split_image_set,
)
from flint.logging import logger
from flint.naming import create_imaging_name_prefix
from flint.options import MS
from flint.utils import get_packaged_resource_path


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


def test_combine_subbands_to_cube(tmpdir):
    """Load in example fits images to combine into a cube"""
    files = [
        get_packaged_resource_path(
            package="flint.data.tests",
            filename=f"SB56659.RACS_0940-04.beam17.round3-000{i}-image.sub.fits",
        )
        for i in range(3)
    ]
    files = [Path(shutil.copy(Path(f), Path(tmpdir))) for f in files]

    assert len(files) == 3
    assert all([f.exists() for f in files])
    file_parent = files[0].parent
    prefix = f"{file_parent}/SB56659.RACS_0940-04.beam17.round3"
    image_set = ImageSet(
        prefix=prefix,
        image=files,
    )

    new_image_set = combine_image_set_to_cube(
        image_set=image_set, remove_original_images=False
    )

    assert new_image_set.prefix == image_set.prefix
    assert len(new_image_set.image) == 1

    with pytest.raises(TypeError):
        _ = combine_image_set_to_cube(image_set=files, remove_original_images=False)  # type: ignore


def test_combine_subbands_to_cube2(tmpdir):
    """Load in example fits images to combine into a cube without deleting original"""
    files = [
        get_packaged_resource_path(
            package="flint.data.tests",
            filename=f"SB56659.RACS_0940-04.beam17.round3-000{i}-image.sub.fits",
        )
        for i in range(3)
    ]
    files = [Path(shutil.copy(Path(f), Path(tmpdir))) for f in files]

    assert len(files) == 3
    assert all([f.exists() for f in files])
    file_parent = files[0].parent
    prefix = f"{file_parent}/SB56659.RACS_0940-04.beam17.round3"
    image_set = ImageSet(
        prefix=prefix,
        image=files,
    )

    new_image_set = combine_image_set_to_cube(
        image_set=image_set, remove_original_images=True
    )
    assert all([not file.exists() for file in files])
    assert new_image_set.prefix == image_set.prefix
    assert len(new_image_set.image) == 1


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


def test_create_wsclean_name(ms_example):
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


def test_create_wsclean_command(ms_example):
    """Test whether WSCleanOptions can be correctly cast to a command string"""
    wsclean_options = WSCleanOptions()

    command = create_wsclean_cmd(
        ms=MS.cast(ms_example), wsclean_options=wsclean_options
    )
    assert isinstance(command, WSCleanResult)


def test_create_wsclean_command_with_environment(ms_example):
    """Test whether WSCleanOptions can be correctly cast to a command string"""
    wsclean_options = WSCleanOptions(temp_dir="$LOCALDIR")

    command = create_wsclean_cmd(
        ms=MS.cast(ms_example), wsclean_options=wsclean_options
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


def test_merge_image_sets():
    """Test merging image sets"""
    image_set = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, include_mfs=False
    )

    image_set2 = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, include_mfs=False
    )

    merged = merge_image_sets(image_sets=[image_set, image_set2])

    assert isinstance(merged, ImageSet)
    assert merged.prefix == "JackSparrow"

    assert merged.image is not None
    assert len(merged.image) == 8
    assert isinstance(merged.image[0], Path)

    assert merged.dirty is not None
    assert len(merged.dirty) == 8
    assert isinstance(merged.dirty[0], Path)

    assert merged.model is not None
    assert len(merged.model) == 8
    assert isinstance(merged.model[0], Path)

    assert merged.residual is not None
    assert len(merged.residual) == 8
    assert isinstance(merged.residual[0], Path)

    assert merged.psf is not None
    assert len(merged.psf) == 8
    assert isinstance(merged.psf[0], Path)


def test_split_image_set():
    ms = Path("SB1234.FieldNme.beam00.round4.ms")
    i_prefix = create_imaging_name_prefix(ms, pol="i")
    q_prefix = create_imaging_name_prefix(ms, pol="q")
    u_prefix = create_imaging_name_prefix(ms, pol="u")
    v_prefix = create_imaging_name_prefix(ms, pol="v")

    i_image_set = get_wsclean_output_names(
        prefix=i_prefix,
        subbands=4,
        include_mfs=False,
        pols="I",
    )
    q_image_set = get_wsclean_output_names(
        prefix=q_prefix,
        subbands=4,
        include_mfs=False,
        pols="Q",
    )
    u_image_set = get_wsclean_output_names(
        prefix=u_prefix,
        subbands=4,
        include_mfs=False,
        pols="U",
    )
    v_image_set = get_wsclean_output_names(
        prefix=v_prefix,
        subbands=4,
        include_mfs=False,
        pols="V",
    )

    image_set = ImageSet(
        prefix=i_prefix,
        image=i_image_set.image
        + q_image_set.image
        + u_image_set.image
        + v_image_set.image,
        dirty=i_image_set.dirty
        + q_image_set.dirty
        + u_image_set.dirty
        + v_image_set.dirty,
        model=i_image_set.model
        + q_image_set.model
        + u_image_set.model
        + v_image_set.model,
        residual=i_image_set.residual
        + q_image_set.residual
        + u_image_set.residual
        + v_image_set.residual,
        psf=i_image_set.psf + q_image_set.psf + u_image_set.psf + v_image_set.psf,
    )

    split_dict = split_image_set(image_set=image_set, by="pol", mode="image")

    logger.info(f"Split dict: {split_dict}")

    assert isinstance(split_dict, dict)
    for s, (key, val) in zip(
        ("i", "q", "u", "v"),
        split_dict.items(),
    ):
        assert isinstance(val, list)
        assert len(val) == 4
        assert isinstance(val[0], Path)
        assert key == s

    with pytest.raises(NamingException):
        split_image_set(image_set=image_set, by="pol", mode="plunder")


def test_split_and_get_image_set():
    ms = Path("SB1234.FieldNme.beam00.round4.ms")
    i_prefix = create_imaging_name_prefix(ms, pol="i")
    q_prefix = create_imaging_name_prefix(ms, pol="q")
    u_prefix = create_imaging_name_prefix(ms, pol="u")
    v_prefix = create_imaging_name_prefix(ms, pol="v")

    i_image_set = get_wsclean_output_names(
        prefix=i_prefix,
        subbands=4,
        include_mfs=False,
        pols="I",
    )
    q_image_set = get_wsclean_output_names(
        prefix=q_prefix,
        subbands=4,
        include_mfs=False,
        pols="Q",
    )
    u_image_set = get_wsclean_output_names(
        prefix=u_prefix,
        subbands=4,
        include_mfs=False,
        pols="U",
    )
    v_image_set = get_wsclean_output_names(
        prefix=v_prefix,
        subbands=4,
        include_mfs=False,
        pols="V",
    )

    image_set = ImageSet(
        prefix=i_prefix,
        image=i_image_set.image
        + q_image_set.image
        + u_image_set.image
        + v_image_set.image,
        dirty=i_image_set.dirty
        + q_image_set.dirty
        + u_image_set.dirty
        + v_image_set.dirty,
        model=i_image_set.model
        + q_image_set.model
        + u_image_set.model
        + v_image_set.model,
        residual=i_image_set.residual
        + q_image_set.residual
        + u_image_set.residual
        + v_image_set.residual,
        psf=i_image_set.psf + q_image_set.psf + u_image_set.psf + v_image_set.psf,
    )

    split_list = split_and_get_image_set(
        image_set=image_set, get="i", by="pol", mode="image"
    )

    assert isinstance(split_list, list)
    assert len(split_list) == 4
    assert isinstance(split_list[0], Path)
    assert split_list[0].name.endswith("I-image.fits")
