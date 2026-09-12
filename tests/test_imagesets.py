"""Test operations around image sets"""

from __future__ import annotations

from pathlib import PosixPath

import pytest

from flint.imager.wsclean import ImageSet, split_and_get_image_set


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
