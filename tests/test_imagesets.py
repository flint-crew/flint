"""Test operations around image sets"""

from __future__ import annotations

from pathlib import PosixPath

import pytest

from flint.imager.wsclean import ImageSet, split_and_get_image_set


@pytest.fixture
def example_image_set() -> ImageSet:
    """Fixture for an example ImageSet object"""
    return ImageSet(
        prefix="/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu",
        image=[
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0000-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0001-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0002-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0003-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0004-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0005-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0006-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0007-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0008-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0009-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0010-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0011-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0012-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0013-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0014-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0015-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0016-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0017-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0018-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0019-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0020-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0021-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0022-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-MFS-Q.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0000-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0001-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0002-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0003-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0004-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0005-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0006-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0007-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0008-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0009-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0010-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0011-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0012-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0013-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0014-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0015-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0016-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0017-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0018-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0019-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0020-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0021-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-0022-U.image.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-MFS-U.image.fits"
            ),
        ],
        psf=None,
        dirty=None,
        model=None,
        residual=[
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-MFS-Q.residual.fits"
            ),
            PosixPath(
                "/scratch3/gal16b/flint_racsall/56289/SB56289.RACS_1041+18.project-pol.beam15.round1.qu-MFS-U.residual.fits"
            ),
        ],
        source_list=None,
    )


def test_split_and_get_image_set(example_image_set: ImageSet) -> None:
    """Split a image set into separate images based on the file name"""
    # Tracing down an error when developing racs-all pipeline. More tests are ok.
    results = split_and_get_image_set(
        image_set=example_image_set,
        get="qu",
        by="pol",
        mode="image",
    )

    assert len(results) == len(example_image_set.image)
