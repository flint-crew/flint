"""Test operations around image sets"""

from __future__ import annotations

import shutil
from pathlib import Path, PosixPath

import pytest

from flint.exceptions import NamingException
from flint.imager.wsclean import (
    get_wsclean_output_names,
    get_wsclean_output_source_list_path,
)
from flint.imageset import (
    ImageSet,
    combine_image_set_to_cube,
    merge_image_sets,
    split_and_get_image_set,
    split_image_set,
    transpose_and_sort_channel_images,
)
from flint.logging import logger
from flint.naming import create_imaging_name_prefix
from flint.options import FitsCubeOptions
from flint.utils import get_packaged_resource_path


@pytest.fixture
def example_image_set() -> ImageSet:
    """Fixture for an example ImageSet object, reflecting the naming produced by a
    joint Stokes Q/U wsclean run once it has gone through
    ``rename_wsclean_prefix_in_image_set`` - wsclean's own per-image ``-Q``/``-U``
    tag is folded into a single-letter ``pol`` field per image (see
    ``_rename_wsclean_title``), rather than every image sharing the joint ``qu`` tag."""
    base = "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1"
    channels = [f"{i:04d}" for i in range(23)] + ["MFS"]
    return ImageSet(
        prefix=f"{base}.qu",
        image=[
            PosixPath(f"{base}.{stokes}.{chan}.image.fits")
            for stokes in ("q", "u")
            for chan in channels
        ],
        psf=None,
        dirty=None,
        model=None,
        residual=[
            PosixPath(f"{base}.{stokes}.MFS.residual.fits") for stokes in ("q", "u")
        ],
        source_list=None,
    )


def test_split_and_get_image_set(example_image_set: ImageSet) -> None:
    """Split an image set and return the images matching a single Stokes, once
    wsclean's own per-image Q/U tag has been folded into the pol field"""
    results = split_and_get_image_set(
        image_set=example_image_set,
        get="q",
        by="pol",
        mode="image",
    )

    assert len(results) == 24


def test_split_and_get_image_set_loop(example_image_set: ImageSet) -> None:
    """Split an image set into separate images based on the file name"""
    # Tracing down an error when developing racs-all pipeline. More tests are ok.
    for stokes in "qu":
        results = split_and_get_image_set(
            image_set=example_image_set,
            get=stokes,
            by="pol",
            mode="image",
        )
        assert len(results) == 24


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
        image_set=image_set,
        fitscube_options=FitsCubeOptions(remove_original_images=False),
    )

    assert new_image_set.prefix == image_set.prefix
    assert len(new_image_set.image) == 1

    with pytest.raises(TypeError):
        _ = combine_image_set_to_cube(
            image_set=files,  # type: ignore
            fitscube_options=FitsCubeOptions(remove_original_images=False),
        )


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
        image_set=image_set,
        fitscube_options=FitsCubeOptions(remove_original_images=True),
    )
    assert all([not file.exists() for file in files])
    assert new_image_set.prefix == image_set.prefix
    assert len(new_image_set.image) == 1


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


def test_merge_image_sets_with_source_list():
    """Test merging image sets. The source list property of the ImageSet is not
    a list type."""
    source_list = get_wsclean_output_source_list_path(name_path="JackSparrow", pol="i")

    image_set = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, include_mfs=False
    )
    image_set = image_set.with_options(source_list=source_list)

    image_set2 = get_wsclean_output_names(
        prefix="JackSparrow", subbands=4, include_mfs=False
    )
    image_set2 = image_set2.with_options(source_list=source_list)

    merged = merge_image_sets(image_sets=[image_set, image_set2])

    assert isinstance(merged, ImageSet)
    assert merged.prefix == "JackSparrow"
    assert merged.source_list is not None
    assert merged.source_list == source_list

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


def test_split_and_get_image_set_by_stokes():
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
