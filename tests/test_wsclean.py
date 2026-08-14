"""Testing some wsclean functionality."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from fitscube.bounding_box import get_common_bounding_box
from fitscube.extract import find_target_axis

from flint.exceptions import (
    AttemptRerunException,
    CleanDivergenceError,
    NamingException,
    ShapeMismatchError,
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
    combine_images_to_cube,
    create_wsclean_cmd,
    create_wsclean_name_argument,
    get_parser,
    get_wsclean_output_names,
    get_wsclean_output_source_list_path,
    merge_image_sets,
    rename_wsclean_prefix_in_image_set,
    rotate_cube,
    split_and_get_image_set,
    split_cube_into_planes,
    split_image_set,
    transpose_and_sort_channel_images,
)
from flint.logging import logger
from flint.naming import create_imaging_name_prefix
from flint.options import MS, FitsCubeOptions
from flint.utils import get_packaged_resource_path


def _write_channel_image(
    path: Path, channel: int, shape: tuple[int, int], nan_border: int = 0
) -> Path:
    """A wsclean-like single channel image, with each pixel uniquely valued so
    that any scrambling of the data is detectable. A ``nan_border`` pixels wide
    border of NaNs may be added to exercise bounding-box trimming."""
    ny, nx = shape
    data = (
        channel * 1000.0
        + np.arange(ny)[:, None] * 10.0
        + np.arange(nx)[None, :].astype(float)
    )
    if nan_border:
        data[:nan_border, :] = np.nan
        data[-nan_border:, :] = np.nan
        data[:, :nan_border] = np.nan
        data[:, -nan_border:] = np.nan
    header = fits.Header(
        {
            "BUNIT": "JY/BEAM",
            "BMAJ": 0.01,
            "BMIN": 0.01,
            "BPA": 0.0,
            "CTYPE1": "RA---SIN",
            "CRVAL1": 180.0,
            "CDELT1": -0.001,
            "CRPIX1": 1.0,
            "CUNIT1": "deg",
            "CTYPE2": "DEC--SIN",
            "CRVAL2": -30.0,
            "CDELT2": 0.001,
            "CRPIX2": 1.0,
            "CUNIT2": "deg",
            "CTYPE3": "FREQ",
            "CRVAL3": 8.0e8 + channel * 1.0e6,
            "CDELT3": 1.0e6,
            "CRPIX3": 1.0,
            "CUNIT3": "Hz",
            "CTYPE4": "STOKES",
            "CRVAL4": 1.0,
            "CDELT4": 1.0,
            "CRPIX4": 1.0,
            "SPECSYS": "TOPOCENT",
        }
    )
    fits.writeto(path, data=data[None, None].astype(np.float32), header=header)
    return path


def _assert_cube_matches_images(cube: Path, images: list[Path]) -> None:
    """Every channel of ``cube``, located via its own WCS, should hold the data
    and the frequency of the matching image, and the cube should be ordered
    (chan, pol, dec, ra) as linmos expects."""
    with fits.open(cube) as hdul:
        header, data = hdul[0].header, hdul[0].data

    freq_axis = find_target_axis(header=header)
    assert freq_axis.axis == header["NAXIS"]
    assert header[f"NAXIS{freq_axis.axis}"] == len(images)
    assert data.shape[0] == len(images)

    for channel, image in enumerate(images):
        plane = np.take(data, channel, axis=data.ndim - freq_axis.axis).squeeze()
        assert np.array_equal(plane, fits.getdata(image).squeeze()), channel
        expected_freq = fits.getheader(image)["CRVAL3"]
        assert freq_axis.crval + channel * freq_axis.cdelt == pytest.approx(
            expected_freq
        ), channel


def test_cube_split_and_recombine_roundtrip(tmpdir) -> None:
    """Splitting a beam cube into planes and cubing those planes again -- as the
    per-channel parallel linmos does -- must keep the data and the WCS describing
    it in step. Guards against an axis rotation not matched by the header swap."""
    tmp_path = Path(tmpdir)
    channels, shape = 3, (500, 700)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=shape,
        )
        for channel in range(channels)
    ]

    beam_cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(
            invalidate_zeros=False, remove_original_images=False
        ),
    )
    _assert_cube_matches_images(cube=beam_cube, images=images)

    planes = split_cube_into_planes(cube=beam_cube)
    for plane, image in zip(planes, images):
        assert np.array_equal(
            fits.getdata(plane).squeeze(), fits.getdata(image).squeeze()
        )

    field_cube = combine_images_to_cube(
        images=planes,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(invalidate_zeros=False),
    )
    _assert_cube_matches_images(cube=field_cube, images=images)


def test_rotate_cube_is_idempotent(tmpdir) -> None:
    """A cube already ordered (chan, pol, dec, ra) must be left alone, else the
    second pass of the per-channel linmos path would rotate it back"""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(5, 7),
        )
        for channel in range(3)
    ]
    cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(invalidate_zeros=False),
    )

    header, data = fits.getheader(cube), fits.getdata(cube)
    rotate_cube(cube)

    assert fits.getheader(cube) == header
    assert np.array_equal(fits.getdata(cube), data)


def test_rotate_cube_single_stokes_is_header_only(tmpdir) -> None:
    """With a length-one stokes axis the bytes on disk do not move, so the
    rotation must be done in the header without reading the cube in."""
    cube = Path(tmpdir) / "single_stokes.fits"
    data = np.arange(1 * 3 * 5 * 7, dtype=np.float32).reshape(1, 3, 5, 7)
    header = fits.getheader(
        _write_channel_image(Path(tmpdir) / "plane.fits", channel=0, shape=(5, 7))
    )
    fits.writeto(cube, data=data, header=header)

    rotate_cube(cube)

    with fits.open(cube) as hdul:
        rotated_header, rotated_data = hdul[0].header, hdul[0].data

    assert (rotated_header["NAXIS3"], rotated_header["NAXIS4"]) == (1, 3)
    assert rotated_header["CTYPE3"] == "STOKES"
    assert rotated_header["CTYPE4"] == "FREQ"
    assert "CUNIT3" not in rotated_header
    assert rotated_header["CUNIT4"] == "Hz"
    assert np.array_equal(rotated_data, np.moveaxis(data, 1, 0))


def test_combine_images_to_cube_shape_mismatch(tmpdir) -> None:
    """Planes are written into the cube at fixed byte offsets, so differing
    pixel grids must be rejected rather than silently scrambled."""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(5, 7) if channel == 0 else (5, 6),
        )
        for channel in range(2)
    ]

    with pytest.raises(ShapeMismatchError):
        combine_images_to_cube(
            images=images,
            prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
            mode="image",
            fitscube_options=FitsCubeOptions(),
        )


def test_combine_images_to_cube_bounding_box_trims(tmpdir) -> None:
    """fitscube_options.bounding_box=True should trim the NaN border shared
    by every channel down to the smallest common valid-pixel extent."""
    tmp_path = Path(tmpdir)
    images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=2,
        )
        for channel in range(3)
    ]

    cube = combine_images_to_cube(
        images=images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(bounding_box=True),
    )

    data = fits.getdata(cube)
    assert data.shape[-2:] == (6, 6)
    assert np.isfinite(data).all()


def test_combine_images_to_cube_bounding_box_override_forces_shared_grid(
    tmpdir,
) -> None:
    """A caller-supplied ``bounding_box`` must override fitscube_options.bounding_box
    so two cubes built from differently-padded inputs (e.g. image and weight
    planes) can still be forced onto an identical pixel grid."""
    tmp_path = Path(tmpdir)
    narrow_border_images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-image.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=2,
        )
        for channel in range(3)
    ]
    wide_border_images = [
        _write_channel_image(
            tmp_path / f"SB1234.RACS_0000-00.beam00.round1-{channel:04d}-weight.fits",
            channel=channel,
            shape=(10, 10),
            nan_border=1,
        )
        for channel in range(3)
    ]

    shared_box = get_common_bounding_box(file_list=narrow_border_images)

    image_cube = combine_images_to_cube(
        images=narrow_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="image",
        fitscube_options=FitsCubeOptions(bounding_box=False),
        bounding_box=shared_box,
    )
    weight_cube = combine_images_to_cube(
        images=wide_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1",
        mode="weight",
        fitscube_options=FitsCubeOptions(
            bounding_box=False, remove_original_images=False
        ),
        bounding_box=shared_box,
    )

    # Left independent, the wide-border cube would trim to (8, 8), not (6, 6)
    independent_weight_cube = combine_images_to_cube(
        images=wide_border_images,
        prefix=f"{tmp_path}/SB1234.RACS_0000-00.beam00.round1.independent",
        mode="weight",
        fitscube_options=FitsCubeOptions(bounding_box=True),
    )

    assert (
        fits.getdata(image_cube).shape
        == fits.getdata(weight_cube).shape
        == (
            3,
            1,
            6,
            6,
        )
    )
    assert fits.getdata(independent_weight_cube).shape[-2:] == (8, 8)


def test_split_cube_into_planes(tmpdir) -> None:
    """A cube should split into flint named planes that can be regrouped across beams"""
    files = [
        Path(
            shutil.copy(
                get_packaged_resource_path(
                    package="flint.data.tests",
                    filename=f"SB56659.RACS_0940-04.beam17.round3-000{i}-image.sub.fits",
                ),
                Path(tmpdir),
            )
        )
        for i in range(3)
    ]
    cube = combine_images_to_cube(
        images=files,
        prefix=f"{tmpdir}/SB56659.RACS_0940-04.beam17.round3",
        mode="image",
        fitscube_options=FitsCubeOptions(),
    )

    planes = split_cube_into_planes(cube=cube)

    assert [plane.name for plane in planes] == [
        f"SB56659.RACS_0940-04.beam17.round3.i.ch{channel:04d}-{channel:04d}.fits"
        for channel in range(3)
    ]
    assert all(plane.exists() for plane in planes)
    # The names remain flint parsable, so may be regrouped per channel across beams
    assert len(transpose_and_sort_channel_images([planes, planes])) == 3

    with pytest.raises(NamingException):
        split_cube_into_planes(cube=Path(shutil.copy(cube, Path(tmpdir) / "bad.fits")))


def test_transpose_and_sort_channel_images() -> None:
    """Per-beam channel image lists should regroup into per-channel beam groups,
    sorted by channel range and independent of the input ordering."""
    base = (
        "SB39400.RACS_0000-123.beam{beam}.round3.i.ch{lo:04d}-{hi:04d}.image.conv.fits"
    )
    channels = [(0, 1), (2, 3), (4, 5)]

    def beam_list(beam: int, order: list[tuple[int, int]]) -> list[Path]:
        return [Path(base.format(beam=beam, lo=lo, hi=hi)) for lo, hi in order]

    # Beam 0 in order, beam 1 shuffled - both must sort to the same channel order
    beam_channel_images = [
        beam_list(0, channels),
        beam_list(1, [channels[2], channels[0], channels[1]]),
    ]

    channel_groups = transpose_and_sort_channel_images(beam_channel_images)

    assert len(channel_groups) == len(channels)
    for group, (lo, hi) in zip(channel_groups, channels):
        assert len(group) == 2
        assert all(f"ch{lo:04d}-{hi:04d}" in str(p) for p in group)
        assert [f"beam{b}" in str(p) for b, p in enumerate(group)] == [True, True]

    with pytest.raises(AssertionError):
        transpose_and_sort_channel_images(
            [beam_list(0, channels), beam_list(1, channels[:2])]
        )


def test_transpose_and_sort_channel_images_wsclean_raw_index() -> None:
    """When channels are imaged individually without being grouped into a flint
    ch<lo>-<hi> range, images only carry wsclean's own bare per-channel index
    (e.g. ``.0000``). This must still be sortable/groupable rather than
    raising - regression test for the "No channel range in path" crash."""
    base = "SB56289.RACS_1041+18.beam{beam:02d}.round1.i.{idx:04d}.image.conv.fits"

    def beam_list(beam: int, order: list[int]) -> list[Path]:
        return [Path(base.format(beam=beam, idx=idx)) for idx in order]

    beam_channel_images = [beam_list(0, [0, 1, 2]), beam_list(1, [2, 0, 1])]

    channel_groups = transpose_and_sort_channel_images(beam_channel_images)

    assert len(channel_groups) == 3
    for idx, group in enumerate(channel_groups):
        assert len(group) == 2
        assert all(f".{idx:04d}.image" in str(p) for p in group)


def test_get_cli_parser() -> None:
    """capn_crunch was throwing error over duplicated options being added to
    the argpase object. No conflicting options means the parser should just
    be returned"""
    _ = get_parser()


def test_rotate_cube_no_exists() -> None:
    """Should no cube exist this should exit safely with a warning.
    This is not testing the actual rotation code, just the early
    return."""
    output_cube_path = Path("JackSparrowIsNotHere.fits")

    _ = rotate_cube(output_cube_path=output_cube_path, inplace=False)
    _ = rotate_cube(output_cube_path=output_cube_path, inplace=True)


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
    # negative lookbehind typo that previously left this as `i-0000-image`
    ex = "SB56289.RACS_1041+18.beam00.round1.i-0000-image.fits"
    out_ex = "SB56289.RACS_1041+18.beam00.round1.i.0000.image.fits"
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

    assert f"{parent}/SB39400.RACS_0635-31.beam0.small.i.pol" == str(name_argument_path)


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
