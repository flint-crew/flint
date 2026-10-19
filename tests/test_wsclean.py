"""Testing some wsclean functionality."""

from __future__ import annotations

import os
import re
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from pydantic import ValidationError

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
    NamingException,
    NotSupportedError,
)
from flint.imager.wsclean import (
    WSCleanOptions,
    WSCleanOutput,
    WSCleanResult,
    _resolve_wsclean_key_value_to_cli_str,
    _wsclean_output_callback,
    clear_wsclean_outputs,
    collect_wsclean_outputs,
    create_wsclean_cmd,
    create_wsclean_name_argument,
    expected_wsclean_outputs,
    find_wsclean_outputs,
    flint_output_path,
    get_parser,
    get_wsclean_output_source_list_path,
    parse_pols,
    parse_wsclean_output,
    remove_wsclean_outputs,
    run_wsclean_imager,
    verify_wsclean_outputs,
    wsclean_output_path,
)
from flint.imageset import split_and_get_image_set
from flint.naming import create_imaging_name_prefix
from flint.options import MS, FitsCubeOptions
from tests.test_cube import _write_channel_image


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


PREFIXES = (
    "SB56289.RACS_1041+18.beam00.round1.i",
    "SB57516.RACS_0929-81.beam35.round4.i.ch0287-0288",
    "SB57516.RACS_0929-81.beam35.round4.i.scan0001-0002",
    "SB56289.RACS_1041+18.project-image.beam15.round1.qu",
    "/a/path/to/SB39400.RACS_0635-31.beam33.xxyy",
)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_wsclean_output_round_trip(prefix: str):
    """Every name the builder makes parses back into the fields it was made from"""
    for (channel, mfs), pol, interval, image_type, primary_beam in product(
        ((None, False), (3, False), (None, True)),
        (None, "I", "Q", "XX", "XYi"),
        (None, 7),
        ("image", "dirty", "model", "residual", "psf"),
        (False, True),
    ):
        fields: dict[str, Any] = {
            "image_type": image_type,
            "channel": channel,
            "mfs": mfs,
            "pol": pol,
            "interval": interval,
            "primary_beam": primary_beam,
        }
        path = wsclean_output_path(prefix=prefix, **fields)
        assert path.parent == Path(prefix).parent
        assert parse_wsclean_output(path=path, prefix=prefix) == WSCleanOutput(
            path=path, **fields
        )


def test_parse_wsclean_output_ignores_other_files():
    """Files that are not wsclean images of this prefix are not parsed"""
    prefix = "SB56289.RACS_1041+18.beam00.round1.i"
    for name in (
        f"{prefix}-sources.txt",
        f"{prefix}-beam-0.fits",
        f"{prefix}-0000-image.sub.fits",
        f"{prefix}.ch0000-0010-image.fits",
        f"{prefix}i-image.fits",
        f"{prefix}-000-image.fits",
        f"{prefix}.MFS.image.fits",
    ):
        assert parse_wsclean_output(path=Path(name), prefix=prefix) is None


def test_parse_wsclean_output_interval_before_channel():
    """wsclean writes the interval ahead of the channel, as t plus four digits"""
    prefix = "SB56289.RACS_1041+18.beam00.round1.i"
    output = parse_wsclean_output(
        path=Path(f"{prefix}-t0005-0002-image.fits"), prefix=prefix
    )
    assert output is not None
    assert output.interval == 5
    assert output.channel == 2


@pytest.mark.parametrize(
    "prefix, wsclean_suffix, pol_token, flint_suffix",
    [
        ("SB39400.RACS_0635-31.beam33.poli", "-MFS-image", "i", ".MFS.image"),
        (
            "SB57516.RACS_0929-81.beam35.round4.i.ch0287-0288",
            "-image",
            "i",
            ".image",
        ),
        ("SB57988.RACS_1415-46.beam34.round4.v", "-MFS-model", "v", ".MFS.model"),
        (
            "SB56289.RACS_1041+18.beam00.round1.i",
            "-0000-image",
            "i",
            ".ch0000-0001.image",
        ),
        (
            "SB56289.RACS_1041+18.beam00.round1.i",
            "-t0005-0000-image",
            "i",
            ".ch0000-0001.scan0005-0006.image",
        ),
        ("SB56289.RACS_1041+18.beam00.round1.i", "-0002-psf", "i", ".ch0002-0003.psf"),
        ("SB56289.RACS_1041+18.beam00.round1.i", "-MFS-image-pb", "i", ".MFS.image.pb"),
    ],
)
def test_flint_output_path_single_pol(
    prefix: str, wsclean_suffix: str, pol_token: str, flint_suffix: str
):
    """A single polarisation keeps the prefix as it is, and gains dot separated fields"""
    output = parse_wsclean_output(
        path=Path(f"/scratch/{prefix}{wsclean_suffix}.fits"), prefix=prefix
    )
    assert output is not None
    assert flint_output_path(output=output, prefix=prefix, pol_token=pol_token) == Path(
        f"/scratch/{prefix}{flint_suffix}.fits"
    )


@pytest.mark.parametrize(
    "token, wsclean_pol, flint_pol",
    [
        ("qu", "Q", "q"),
        ("qu", "U", "u"),
        ("iquv", "I", "i"),
        ("iquv", "V", "v"),
        ("xxyy", "XX", "xx"),
        ("xxyy", "YY", "yy"),
    ],
)
def test_flint_output_path_joint_pol(token: str, wsclean_pol: str, flint_pol: str):
    """The image's own polarisation replaces the field naming every polarisation imaged"""
    base = "SB56289.RACS_1041+18.beam15.round1"
    prefix = f"{base}.{token}"
    for wsclean_suffix, flint_suffix in (
        (f"-0000-{wsclean_pol}-image", ".ch0000-0001.image"),
        (f"-MFS-{wsclean_pol}-residual", ".MFS.residual"),
        (f"-{wsclean_pol}-image", ".image"),
    ):
        output = parse_wsclean_output(
            path=Path(f"{prefix}{wsclean_suffix}.fits"), prefix=prefix
        )
        assert output is not None
        assert flint_output_path(output=output, prefix=prefix, pol_token=token) == Path(
            f"{base}.{flint_pol}{flint_suffix}.fits"
        )


def test_flint_output_path_joint_pol_with_channel_range():
    """The polarisation field is found even when a channel range follows it"""
    prefix = "SB56289.RACS_1041+18.beam15.round1.qu.ch0010-0020"
    output = parse_wsclean_output(
        path=Path(f"{prefix}-0001-U-image.fits"), prefix=prefix
    )
    assert output is not None
    assert flint_output_path(output=output, prefix=prefix, pol_token="qu") == Path(
        "SB56289.RACS_1041+18.beam15.round1.u.ch0010-0020.ch0001-0002.image.fits"
    )


def test_flint_output_path_joint_pol_psf():
    """The PSF is shared by every polarisation, so keeps the joint field"""
    prefix = "SB56289.RACS_1041+18.beam15.round1.qu"
    output = parse_wsclean_output(path=Path(f"{prefix}-0001-psf.fits"), prefix=prefix)
    assert output is not None
    assert flint_output_path(output=output, prefix=prefix, pol_token="qu") == Path(
        f"{prefix}.ch0001-0002.psf.fits"
    )


def test_flint_output_path_missing_pol_token():
    prefix = "SB56289.RACS_1041+18.beam15.round1.i"
    output = parse_wsclean_output(
        path=Path(f"{prefix}-0001-Q-image.fits"), prefix=prefix
    )
    assert output is not None
    with pytest.raises(NamingException):
        flint_output_path(output=output, prefix=prefix, pol_token="qu")


@pytest.mark.parametrize(
    "project",
    ["pol", "image", "dirty", "model", "residual", "psf", "MFS", "i", "q", "jack"],
)
def test_flint_output_path_project_field_collision(project: str):
    """A project name matching a wsclean token is part of the prefix, so is left alone"""
    prefix = f"SB56289.RACS_1041+18.project-{project}.beam15.round1.i"
    output = parse_wsclean_output(path=Path(f"{prefix}-0000-image.fits"), prefix=prefix)
    assert output is not None
    assert flint_output_path(output=output, prefix=prefix, pol_token="i") == Path(
        f"{prefix}.ch0000-0001.image.fits"
    )


def test_expected_wsclean_outputs_with_pols():
    paths = expected_wsclean_outputs(
        prefix="JackSparrow", channels_out=4, pols=("I", "Q"), image_types=("image",)
    )
    assert len(paths) == 10
    assert paths[0] == Path("JackSparrow-0000-I-image.fits")
    assert paths[4] == Path("JackSparrow-MFS-I-image.fits")
    assert paths[5] == Path("JackSparrow-0000-Q-image.fits")
    assert paths[9] == Path("JackSparrow-MFS-Q-image.fits")

    psfs = expected_wsclean_outputs(
        prefix="JackSparrow", channels_out=4, pols=("I", "Q"), image_types=("psf",)
    )
    assert psfs == [Path(f"JackSparrow-{c:04d}-psf.fits") for c in range(4)] + [
        Path("JackSparrow-MFS-psf.fits")
    ]


def test_expected_wsclean_outputs_single_channel():
    """Imaging one channel adds no channel field, and there is no MFS image"""
    paths = expected_wsclean_outputs(
        prefix="JackSparrow", channels_out=1, pols=None, image_types=("image", "psf")
    )
    assert paths == [Path("JackSparrow-image.fits"), Path("JackSparrow-psf.fits")]


def _touch_wsclean_outputs(paths: list[Path]) -> list[Path]:
    for path in paths:
        path.write_text(path.name)
    return paths


def test_find_wsclean_outputs(tmp_path: Path):
    """Only this run's exact names come back, ordered by channel with MFS last.
    Images a run with other settings would write are ignored."""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.qu"
    expected = _touch_wsclean_outputs(
        expected_wsclean_outputs(
            prefix=prefix,
            channels_out=3,
            pols=("Q", "U"),
            image_types=("image", "residual", "psf"),
        )
    )
    _touch_wsclean_outputs(
        [
            Path(f"{prefix}-sources.txt"),
            Path(f"{prefix}.ch0000-0010-0000-Q-image.fits"),
            Path(f"{prefix}-0003-Q-image.fits"),
            Path(f"{prefix}-MFS-V-image.fits"),
            tmp_path / "SB56289.RACS_1041+18.beam15.round1.i-0000-image.fits",
        ]
    )

    outputs = find_wsclean_outputs(prefix=prefix, channels_out=3, pols=("Q", "U"))
    assert sorted(output.path for output in outputs) == sorted(expected)

    q_images = [
        output.path.name
        for output in outputs
        if output.image_type == "image" and output.pol == "Q"
    ]
    assert q_images == [
        f"{prefix.name}-{c}-Q-image.fits" for c in ("0000", "0001", "0002", "MFS")
    ]


def test_verify_wsclean_outputs(tmp_path: Path):
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.qu"
    paths = _touch_wsclean_outputs(
        expected_wsclean_outputs(prefix=prefix, channels_out=3, pols=("Q", "U"))
    )
    verify_wsclean_outputs(
        outputs=find_wsclean_outputs(prefix=prefix, channels_out=3, pols=("Q", "U")),
        prefix=prefix,
        channels_out=3,
        pols=("Q", "U"),
    )

    paths[1].unlink()
    with pytest.raises(FileNotFoundError, match=re.escape(paths[1].name)):
        verify_wsclean_outputs(
            outputs=find_wsclean_outputs(
                prefix=prefix, channels_out=3, pols=("Q", "U")
            ),
            prefix=prefix,
            channels_out=3,
            pols=("Q", "U"),
        )


def test_clear_wsclean_outputs(tmp_path: Path):
    """Every image this run writes is cleared if it exists, and nothing else"""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.i"
    earlier = _touch_wsclean_outputs(
        expected_wsclean_outputs(
            prefix=prefix,
            channels_out=2,
            pols=None,
            image_types=("image", "dirty", "model", "residual", "psf"),
        )
    )
    others = _touch_wsclean_outputs(
        [
            Path(f"{prefix}-sources.txt"),
            Path(f"{prefix}-0002-image.fits"),
            Path(f"{prefix}.MFS.image.fits"),
        ]
    )

    removed = clear_wsclean_outputs(prefix=prefix, channels_out=2, pols=None)

    assert sorted(removed) == sorted(earlier)
    assert not any(path.exists() for path in earlier)
    assert all(path.exists() for path in others)


def test_remove_wsclean_outputs(tmp_path: Path):
    """Channel images of the named types go, MFS images and the science images stay"""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.i"
    _touch_wsclean_outputs(
        expected_wsclean_outputs(
            prefix=prefix,
            channels_out=2,
            pols=None,
            image_types=("image", "residual", "psf", "model"),
        )
    )

    removed = remove_wsclean_outputs(
        outputs=find_wsclean_outputs(prefix=prefix, channels_out=2, pols=None)
    )
    assert sorted(path.name for path in removed) == sorted(
        f"{prefix.name}-{c:04d}-{image_type}.fits"
        for c in range(2)
        for image_type in ("residual", "psf", "model")
    )
    remaining = {
        output.path.name
        for output in find_wsclean_outputs(prefix=prefix, channels_out=2, pols=None)
    }
    assert remaining == {
        f"{prefix.name}-0000-image.fits",
        f"{prefix.name}-0001-image.fits",
        f"{prefix.name}-MFS-image.fits",
        f"{prefix.name}-MFS-residual.fits",
        f"{prefix.name}-MFS-psf.fits",
        f"{prefix.name}-MFS-model.fits",
    }


def _wsclean_result_for(prefix: Path, **options: Any) -> WSCleanResult:
    ms = MS(path=prefix.parent / "SB56289.RACS_1041+18.beam15.round1.ms")
    return WSCleanResult(
        cmd="wsclean",
        options=WSCleanOptions(**options),
        ms=ms,
        ms_list=[ms],
        bind_dirs=(),
        move_hold_directories=(prefix.parent, prefix.parent),
        image_prefix_str=str(prefix),
    )


def _write_wsclean_images(
    prefix: Path, channels_out: int, pols: tuple[str, ...] | None
) -> None:
    for pol in pols or (None,):
        for channel in range(channels_out):
            for image_type in ("image", "residual"):
                _write_channel_image(
                    path=wsclean_output_path(
                        prefix=prefix, image_type=image_type, channel=channel, pol=pol
                    ),
                    channel=channel,
                    shape=(8, 8),
                )
        for image_type in ("image", "residual"):
            _write_channel_image(
                path=wsclean_output_path(
                    prefix=prefix, image_type=image_type, mfs=True, pol=pol
                ),
                channel=0,
                shape=(8, 8),
            )


def test_collect_wsclean_outputs_stokes_i(tmp_path: Path):
    """Images are renamed to flint's naming and the channels cubed, under the
    same names as before images were renamed ahead of cubing"""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.i"
    _write_wsclean_images(prefix=prefix, channels_out=3, pols=None)

    image_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(prefix=prefix, channels_out=3, pol="i"),
        prefix=str(prefix),
        fitscube_options=FitsCubeOptions(),
    )

    assert image_set.prefix == str(prefix)
    assert image_set.image == [
        tmp_path / f"{prefix.name}.image.cube.fits",
        tmp_path / f"{prefix.name}.MFS.image.fits",
    ]
    assert image_set.residual == [
        tmp_path / f"{prefix.name}.residual.cube.fits",
        tmp_path / f"{prefix.name}.MFS.residual.fits",
    ]
    assert all(path.exists() for path in image_set.image + image_set.residual)
    assert not list(tmp_path.glob("*-image.fits"))


def test_collect_wsclean_outputs_joint_pol(tmp_path: Path):
    """Each polarisation of a joint run is named by its own field"""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.qu"
    _write_wsclean_images(prefix=prefix, channels_out=2, pols=("Q", "U"))

    image_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(prefix=prefix, channels_out=2, pol="qu"),
        prefix=str(prefix),
        make_cube_from_subbands=False,
    )

    base = "SB56289.RACS_1041+18.beam15.round1"
    assert [path.name for path in image_set.image] == [
        f"{base}.q.ch0000-0001.image.fits",
        f"{base}.q.ch0001-0002.image.fits",
        f"{base}.q.MFS.image.fits",
        f"{base}.u.ch0000-0001.image.fits",
        f"{base}.u.ch0001-0002.image.fits",
        f"{base}.u.MFS.image.fits",
    ]
    assert len(split_and_get_image_set(image_set=image_set, get="u")) == 3


def test_collect_wsclean_outputs_missing_images(tmp_path: Path):
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.i"
    _write_wsclean_images(prefix=prefix, channels_out=3, pols=None)
    wsclean_output_path(prefix=prefix, image_type="image", channel=1).unlink()

    with pytest.raises(FileNotFoundError):
        collect_wsclean_outputs(
            wsclean_result=_wsclean_result_for(prefix=prefix, channels_out=3, pol="i"),
            prefix=str(prefix),
        )


def test_collect_wsclean_outputs_ignores_earlier_run(tmp_path: Path):
    """A single channel run gathers only its own image, leaving those a four
    channel run wrote into the same directory alone"""
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.i"
    _write_wsclean_images(prefix=prefix, channels_out=4, pols=None)
    earlier = sorted(tmp_path.iterdir())
    for image_type in ("image", "residual"):
        _write_channel_image(
            path=wsclean_output_path(prefix=prefix, image_type=image_type),
            channel=0,
            shape=(8, 8),
        )

    image_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(prefix=prefix, channels_out=1, pol="i"),
        prefix=str(prefix),
    )

    assert image_set.image == [tmp_path / f"{prefix.name}.image.fits"]
    assert all(path.exists() for path in earlier)


def test_run_wsclean_imager_clears_earlier_outputs(tmp_path: Path, monkeypatch):
    """Images an earlier attempt left in the hold or MS directory are cleared
    before wsclean runs, so the run's own images are moved in and gathered"""
    hold_dir, move_dir = tmp_path / "hold", tmp_path / "move"
    hold_dir.mkdir()
    move_dir.mkdir()
    name = "SB56289.RACS_1041+18.beam15.round1.i"

    # Same names as this run's outputs, with data a fresh run would not write
    for path in (
        wsclean_output_path(prefix=move_dir / name, image_type="image", mfs=True),
        wsclean_output_path(prefix=hold_dir / name, image_type="residual", channel=0),
    ):
        _write_channel_image(path=path, channel=99, shape=(8, 8))
    other_settings = _write_channel_image(
        path=wsclean_output_path(prefix=move_dir / name, image_type="image", channel=5),
        channel=99,
        shape=(8, 8),
    )

    monkeypatch.setattr(
        "flint.imager.wsclean.run_singularity_command",
        lambda **kwargs: _write_wsclean_images(
            prefix=hold_dir / name, channels_out=2, pols=None
        ),
    )
    ms = MS(path=move_dir / "SB56289.RACS_1041+18.beam15.round1.ms")
    image_set = run_wsclean_imager(
        wsclean_result=WSCleanResult(
            cmd="wsclean",
            options=WSCleanOptions(channels_out=2, pol="i"),
            ms=ms,
            ms_list=[ms],
            bind_dirs=(),
            move_hold_directories=(move_dir, hold_dir),
            image_prefix_str=str(hold_dir / name),
        ),
        container=tmp_path / "wsclean.sif",
        make_cube_from_subbands=False,
    )

    mfs_image = move_dir / f"{name}.MFS.image.fits"
    assert mfs_image in image_set.image
    assert fits.getdata(mfs_image).squeeze()[0, 0] == 0.0
    assert other_settings.exists()
    assert other_settings not in image_set.image


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


@pytest.mark.parametrize(
    "pol, pols",
    [
        ("i", None),
        ("v", None),
        ("qu", ("Q", "U")),
        ("q,u", ("Q", "U")),
        ("iquv", ("I", "Q", "U", "V")),
        ("xx,yy", ("XX", "YY")),
        ("XX,YY", ("XX", "YY")),
    ],
)
def test_parse_pols(pol: str, pols: tuple[str, ...] | None):
    assert parse_pols(pol_str=pol) == pols


@pytest.mark.parametrize(
    "pol",
    [
        "xx",
        "yy,xx",
        "xx,xy,yx,yy",
        "rr,ll",
        "qux",
        "ix",
        "",
        "i,",
        ",i",
        "i,,q",
        "q,u,",
    ],
)
def test_parse_pols_unsupported(pol: str):
    """Every comma separated part must name a polarisation, and only XX and YY
    together are taken of the feed products, as Stokes I is formed from them"""
    with pytest.raises(NotSupportedError):
        parse_pols(pol_str=pol)
    with pytest.raises(ValidationError):
        WSCleanOptions(pol=pol)


def test_wsclean_options_xx_yy_refuses_source_list():
    assert WSCleanOptions(pol="xx,yy").pol == "xx,yy"
    with pytest.raises(ValidationError):
        WSCleanOptions(pol="xx,yy", save_source_list=True)
    with pytest.raises(ValidationError):
        WSCleanOptions(save_source_list=True).with_options(pol="xx,yy")


def _set_feed(path: Path, scale: float, stokes: float) -> None:
    """Scale an image's pixels, and set the polarisation its STOKES axis names"""
    with fits.open(path, mode="update") as open_fits:
        open_fits[0].data *= scale
        open_fits[0].header["CRVAL4"] = stokes


def _write_xx_yy_images(prefix: Path, channels_out: int) -> None:
    """XX and YY images where YY is three times XX, so I is twice XX. Like
    wsclean, the MFS model and dirty images and the shared PSF are written too."""
    _write_wsclean_images(prefix=prefix, channels_out=channels_out, pols=("XX", "YY"))
    for pol in ("XX", "YY"):
        for image_type in ("model", "dirty"):
            _write_channel_image(
                path=wsclean_output_path(
                    prefix=prefix, image_type=image_type, mfs=True, pol=pol
                ),
                channel=0,
                shape=(8, 8),
            )
    _write_channel_image(
        path=wsclean_output_path(prefix=prefix, image_type="psf", mfs=True),
        channel=0,
        shape=(8, 8),
    )
    for output in find_wsclean_outputs(
        prefix=prefix, channels_out=channels_out, pols=("XX", "YY")
    ):
        if output.pol == "XX":
            _set_feed(path=output.path, scale=1.0, stokes=-5.0)
        elif output.pol == "YY":
            _set_feed(path=output.path, scale=3.0, stokes=-8.0)


def test_collect_wsclean_outputs_xx_yy(tmp_path: Path):
    """XX and YY become Stokes I named just as a Stokes I run would be, and the
    channel cube holds only Stokes I"""
    i_dir, linear_dir = tmp_path / "i", tmp_path / "linear"
    i_dir.mkdir()
    linear_dir.mkdir()
    base = "SB56289.RACS_1041+18.beam15.round1"

    _write_wsclean_images(prefix=i_dir / f"{base}.i", channels_out=3, pols=None)
    i_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(
            prefix=i_dir / f"{base}.i", channels_out=3, pol="i"
        ),
        prefix=str(i_dir / f"{base}.i"),
    )

    linear_prefix = linear_dir / f"{base}.xxyy"
    _write_xx_yy_images(prefix=linear_prefix, channels_out=3)
    linear_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(
            prefix=linear_prefix, channels_out=3, pol="xx,yy"
        ),
        prefix=str(linear_prefix),
    )

    assert linear_set.prefix == str(linear_dir / f"{base}.i")
    for mode in ("image", "residual"):
        assert [path.name for path in getattr(linear_set, mode)] == [
            path.name for path in getattr(i_set, mode)
        ]

    cube = linear_set.image[0]
    with fits.open(cube) as open_fits:
        header, data = open_fits[0].header, open_fits[0].data
    assert data.shape[0] == 3
    stokes_axis = next(
        axis
        for axis in range(1, header["NAXIS"] + 1)
        if header[f"CTYPE{axis}"] == "STOKES"
    )
    assert header[f"CRVAL{stokes_axis}"] == 1.0
    np.testing.assert_allclose(data, 2 * fits.getdata(i_set.image[0]))

    mfs = linear_set.image[1]
    assert fits.getheader(mfs)["CRVAL4"] == 1.0
    # Nothing of XX or YY is left, collected or not, while the shared PSF stays
    for pattern in ("*.xx.*", "*.yy.*", "*-XX-*", "*-YY-*"):
        assert not list(linear_dir.glob(pattern)), pattern
    assert wsclean_output_path(
        prefix=linear_prefix, image_type="psf", mfs=True
    ).exists()


def test_collect_wsclean_outputs_keeps_linear_images(tmp_path: Path):
    prefix = tmp_path / "SB56289.RACS_1041+18.beam15.round1.xxyy"
    _write_xx_yy_images(prefix=prefix, channels_out=2)
    image_set = collect_wsclean_outputs(
        wsclean_result=_wsclean_result_for(
            prefix=prefix, channels_out=2, pol="xx,yy", flint_keep_linear_images=True
        ),
        prefix=str(prefix),
        make_cube_from_subbands=False,
    )

    base = "SB56289.RACS_1041+18.beam15.round1"
    assert [path.name for path in image_set.image] == [
        f"{base}.i.ch0000-0001.image.fits",
        f"{base}.i.ch0001-0002.image.fits",
        f"{base}.i.MFS.image.fits",
    ]
    for pol in ("xx", "yy"):
        assert (tmp_path / f"{base}.{pol}.MFS.image.fits").exists()
    for pol in ("XX", "YY"):
        assert wsclean_output_path(
            prefix=prefix, image_type="model", mfs=True, pol=pol
        ).exists()
