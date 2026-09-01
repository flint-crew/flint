"""Tests around the 'natural' resolution convolution the polarisation flow uses:
one common beam per channel, rather than the single 'total' beam RM-synthesis
brings its own inputs to.
"""

from __future__ import annotations

from pathlib import Path

import astropy.units as u
import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from prefect import flow
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness
from radio_beam import Beam

from flint.prefect.common.imaging import (
    convolve_channel_groups_to_natural_resolution,
    convolve_mfs_beam_images_to_common_resolution,
)


def _write_image_with_beam(path: Path, bmaj_arcsec: float) -> Path:
    """A small ASKAP-shaped (stokes, dec, ra) plane carrying the requested beam"""
    wcs = WCS(naxis=3)
    wcs.wcs.ctype = ["RA---SIN", "DEC--SIN", "STOKES"]
    wcs.wcs.crval = [180.0, -30.0, 1.0]
    wcs.wcs.crpix = [8, 8, 1]
    wcs.wcs.cdelt = [-1e-3, 1e-3, 1.0]
    wcs.wcs.cunit = ["deg", "deg", ""]

    rng = np.random.default_rng(0)
    header = wcs.to_header()
    header["BUNIT"] = "Jy/beam"
    beam = Beam(
        major=bmaj_arcsec * u.arcsec, minor=bmaj_arcsec * 0.8 * u.arcsec, pa=0.0 * u.deg
    )
    header = beam.attach_to_header(header)

    fits.writeto(
        path,
        data=rng.normal(0, 1e-3, (1, 16, 16)).astype(np.float32),
        header=header,
        overwrite=True,
    )
    return path


def _bmaj_arcsec(path: Path) -> float:
    return float(fits.getheader(path)["BMAJ"]) * 3600.0


def _channel_groups(
    tmp_path: Path, beams_per_stokes: dict[str, list[list[float]]]
) -> dict[str, list[list[Path]]]:
    """For each Stokes, the per-beam images of each channel, at the given majors"""
    return {
        stokes: [
            [
                _write_image_with_beam(
                    tmp_path / f"{stokes}.ch{channel}.beam{beam}.fits", bmaj_arcsec
                )
                for beam, bmaj_arcsec in enumerate(channel_beams)
            ]
            for channel, channel_beams in enumerate(channels)
        ]
        for stokes, channels in beams_per_stokes.items()
    }


def _convolved_naturally(
    stokes_channel_groups: dict[str, list[list[Path]]],
    cutoff: float | None = None,
    fixed_beam_shape: tuple[float, float, float] | None = None,
) -> dict[str, list[list[Path]]]:
    @flow
    def _convolve() -> dict[str, list[list[Path]]]:
        return convolve_channel_groups_to_natural_resolution(
            stokes_channel_groups=stokes_channel_groups,
            cutoff=cutoff,
            fixed_beam_shape=fixed_beam_shape,
        )

    with prefect_test_harness(), disable_run_logger():
        return _convolve()


def test_natural_resolution_varies_by_channel(tmp_path: Path) -> None:
    """Each channel is brought to its own common beam, so a fine channel is not
    dragged out to the resolution of a coarse one elsewhere in the band"""
    groups = _channel_groups(
        tmp_path, {"q": [[10.0, 10.5], [20.0, 20.5]], "u": [[10.2, 10.1], [20.2, 20.1]]}
    )

    convolved = _convolved_naturally(groups)

    assert set(convolved) == {"q", "u"}
    channel_majors = [
        {
            _bmaj_arcsec(image)
            for images in (convolved["q"][channel], convolved["u"][channel])
            for image in images
        }
        for channel in range(2)
    ]
    # One resolution within a channel, across every beam and both Stokes
    assert all(len(majors) == 1 for majors in channel_majors)
    # But a different one between channels, which is what 'natural' means
    fine, coarse = (majors.pop() for majors in channel_majors)
    assert fine < coarse
    assert fine == pytest.approx(10.6, abs=0.15)
    assert coarse == pytest.approx(20.6, abs=0.15)


def test_natural_resolution_shares_a_beam_across_stokes(tmp_path: Path) -> None:
    """The beam of a channel is solved over every Stokes at that channel, so a
    per-channel polarisation product is meaningful. A Stokes solved on its own
    would land on its own resolution."""
    groups = _channel_groups(tmp_path, {"q": [[10.0]], "u": [[18.0]]})

    convolved = _convolved_naturally(groups)

    assert _bmaj_arcsec(convolved["q"][0][0]) == pytest.approx(
        _bmaj_arcsec(convolved["u"][0][0])
    )
    assert _bmaj_arcsec(convolved["q"][0][0]) >= 18.0, (
        "the shared beam must cover the coarsest Stokes of the channel"
    )


def test_natural_resolution_blanks_a_channel_beyond_the_cutoff(tmp_path: Path) -> None:
    """A channel whose every image is beyond the cutoff has no beam to convolve
    to. It is blanked and marked as holding no PSF, rather than co-added at its
    own differing resolutions under a beam that describes none of them."""
    groups = _channel_groups(
        tmp_path, {"q": [[10.0, 10.5], [40.0, 50.0]], "u": [[10.2, 10.1], [45.0, 55.0]]}
    )

    convolved = _convolved_naturally(groups, cutoff=20.0)

    for stokes in ("q", "u"):
        for image in convolved[stokes][0]:
            assert np.isfinite(fits.getdata(image)).any()
            assert _bmaj_arcsec(image) > 0.0
        for image in convolved[stokes][1]:
            assert np.all(np.isnan(fits.getdata(image)))
            assert fits.getheader(image)["BMAJ"] == 0.0


def test_natural_resolution_honours_a_fixed_beam(tmp_path: Path) -> None:
    """A fixed beam shape overrides the per-channel solve, as it did the single
    beam it replaces"""
    groups = _channel_groups(tmp_path, {"q": [[10.0], [20.0]]})

    convolved = _convolved_naturally(groups, fixed_beam_shape=(25.0, 25.0, 0.0))

    assert [_bmaj_arcsec(images[0]) for images in convolved["q"]] == [
        pytest.approx(25.0),
        pytest.approx(25.0),
    ]


def test_natural_resolution_rejects_mismatched_channel_counts(tmp_path: Path) -> None:
    """Every Stokes has to contribute the same channels, or a channel's beam
    would be solved over the wrong frequencies"""
    groups = _channel_groups(tmp_path, {"q": [[10.0], [20.0]], "u": [[10.0]]})

    with pytest.raises(AssertionError, match="differing channel counts"):
        _convolved_naturally(groups)


def _convolved_mfs(
    mfs_beam_images: dict[str, dict[str, list[Path]]],
    cutoff: float | None = None,
) -> dict[str, dict[str, list[Path]]]:
    @flow
    def _convolve() -> dict[str, dict[str, list[Path]]]:
        return convolve_mfs_beam_images_to_common_resolution(
            mfs_beam_images=mfs_beam_images, cutoff=cutoff
        )

    with prefect_test_harness(), disable_run_logger():
        return _convolve()


def test_mfs_products_share_one_beam(tmp_path: Path) -> None:
    """An MFS product has no frequency axis for a natural beam to vary over, so
    one beam covers every beam, Stokes and product type"""
    # wsclean gives a residual the same restoring beam as its image, so the beam
    # solved over the image products covers the residuals it is applied to. The
    # coarsest, 12.5, is a Stokes Q beam rather than a Stokes I one.
    beams_per_stokes = {
        "i": {"image": [10.0, 11.0], "residual": [10.0, 11.0]},
        "q": {"image": [12.0, 12.5], "residual": [12.0, 12.5]},
    }
    mfs_beam_images = {
        stokes: {
            product_type: [
                _write_image_with_beam(
                    tmp_path / f"{stokes}.{product_type}.beam{beam}.fits", bmaj_arcsec
                )
                for beam, bmaj_arcsec in enumerate(majors)
            ]
            for product_type, majors in product_type_majors.items()
        }
        for stokes, product_type_majors in beams_per_stokes.items()
    }

    convolved = _convolved_mfs(mfs_beam_images)

    majors = {
        _bmaj_arcsec(image)
        for product_type_images in convolved.values()
        for images in product_type_images.values()
        for image in images
    }
    assert len(majors) == 1, "the MFS products landed on differing resolutions"
    assert majors.pop() >= 12.5, "the one beam must cover the coarsest MFS image"


def test_mfs_products_without_images_are_left_alone(tmp_path: Path) -> None:
    """No MFS science image means no beam to solve, so nothing is convolved"""
    assert _convolved_mfs({}) == {}
