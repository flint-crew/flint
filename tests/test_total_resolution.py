"""Tests around the 'total' resolution cubes the polarisation flow writes
alongside the natural ones: a single beam covering the whole band, which is what
RM-synthesis needs of its inputs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from prefect import flow
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.convol import (
    BeamShape,
    convolve_plane_to_beam,
    cubes_share_common_beam,
    get_common_beam,
)
from flint.imager.wsclean import combine_images_to_cube, split_cube_into_planes
from flint.naming import ResolutionModes
from flint.options import FitsCubeOptions
from flint.prefect.common.imaging import (
    _resolution_suffix,
    convolve_channel_groups_to_natural_resolution,
    total_beam_from_channel_groups,
)

# Reuse the synthetic helpers rather than keeping a second copy of them
from tests.test_convol import _write_cube_with_beam  # noqa: E402
from tests.test_natural_resolution import _bmaj_arcsec, _channel_groups  # noqa: E402

STEM = "SB12345.BENCH_0000+00.ch0000-0019"


def _total_beam(
    stokes_channel_groups: dict[str, list[list[Path]]],
    cutoff: float | None = None,
    fixed_beam_shape: tuple[float, float, float] | None = None,
) -> BeamShape:
    @flow
    def _solve() -> BeamShape:
        return total_beam_from_channel_groups(
            stokes_channel_groups=stokes_channel_groups,
            cutoff=cutoff,
            fixed_beam_shape=fixed_beam_shape,
        ).result()

    with prefect_test_harness(), disable_run_logger():
        return _solve()


def _naturally_then_totally(
    stokes_channel_groups: dict[str, list[list[Path]]],
    cutoff: float | None = None,
) -> tuple[dict[str, list[list[Path]]], BeamShape]:
    """The order the polarisation flow runs them in: natural first, then the one
    beam covering the band solved over the result"""

    @flow
    def _run() -> tuple[dict[str, list[list[Path]]], BeamShape]:
        natural = convolve_channel_groups_to_natural_resolution(
            stokes_channel_groups=stokes_channel_groups, cutoff=cutoff
        )
        return natural, total_beam_from_channel_groups(
            stokes_channel_groups=natural, cutoff=cutoff
        ).result()

    with prefect_test_harness(), disable_run_logger():
        return _run()


def _cube_from_planes(planes: list[Path], prefix: Path) -> Path:
    return combine_images_to_cube(
        images=planes,
        prefix=str(prefix),
        mode="image",
        fitscube_options=FitsCubeOptions(
            bounding_box=False,
            create_blanks=False,
            invalidate_zeros=False,
            compress=False,
            remove_original_images=False,
        ),
    )


def test_total_beam_covers_the_coarsest_channel(tmp_path: Path) -> None:
    """One beam for the whole band has to cover every channel in it, or a channel
    it does not cover cannot be deconvolved to reach it"""
    groups = _channel_groups(tmp_path, {"q": [[10.0], [20.0], [15.0]]})

    beam = _total_beam(groups)

    assert beam.bmaj_arcsec >= 20.0


def test_total_beam_covers_every_stokes(tmp_path: Path) -> None:
    """The beam is solved over a single Stokes, which is only sound because the
    natural pass has already given every Stokes of a channel one resolution. Run
    in that order, the total beam still has to cover the coarsest Stokes."""
    groups = _channel_groups(tmp_path, {"q": [[10.0], [12.0]], "u": [[10.0], [22.0]]})

    natural, beam = _naturally_then_totally(groups)

    # Stokes U is the coarsest at channel 1, and the natural pass has already
    # carried Stokes Q out to meet it
    assert beam.bmaj_arcsec >= 22.0
    assert _bmaj_arcsec(natural["q"][1][0]) == pytest.approx(
        _bmaj_arcsec(natural["u"][1][0])
    )


def _planes_at_total_resolution(
    tmp_path: Path, channel_beams: list[float], cutoff: float | None = None
) -> tuple[list[Path], BeamShape]:
    """A natural-resolution cube split into planes and each brought to the one
    beam covering the band, which is what the polarisation flow does to a
    co-added plane before stacking it into the total cube"""
    cube = _write_cube_with_beam(tmp_path / f"{STEM}.q.linmos.fits", channel_beams)
    planes = split_cube_into_planes(cube=cube, output_path=tmp_path / "planes")

    total_beam = get_common_beam(image_paths=planes, cutoff=cutoff)
    convolved = [
        convolve_plane_to_beam(
            plane=plane,
            beam_shape=total_beam,
            cutoff=cutoff,
            convol_suffix=ResolutionModes.TOTAL,
        )
        for plane in planes
    ]
    return convolved, total_beam


def test_total_resolution_cubes_share_one_beam(tmp_path: Path) -> None:
    """The point of the whole change: a cube convolved to the total beam as it is
    built must satisfy the check rm-synth runs on its inputs, so that stage passes
    it straight through instead of convolving it all over again."""
    planes, _ = _planes_at_total_resolution(tmp_path, [10.0, 20.0, 15.0])

    cube = _cube_from_planes(planes=planes, prefix=tmp_path / f"{STEM}.q.total")

    assert cubes_share_common_beam(cube_paths=[cube]), (
        "rm-synth would convolve this all over again"
    )


def test_a_channel_beyond_the_cutoff_is_blanked_not_convolved(tmp_path: Path) -> None:
    """A coarse channel must not drag the whole band out to its resolution. It is
    blanked and marked as holding no PSF, which is what keeps the cube counting as
    sharing a common beam."""
    planes, total_beam = _planes_at_total_resolution(
        tmp_path, [10.0, 40.0], cutoff=20.0
    )

    assert total_beam.bmaj_arcsec < 40.0, "the coarse channel dragged the beam out"
    assert fits.getheader(planes[1])["BMAJ"] == 0.0
    assert np.all(np.isnan(fits.getdata(planes[1])))

    cube = _cube_from_planes(planes=planes, prefix=tmp_path / f"{STEM}.q.total")
    assert cubes_share_common_beam(cube_paths=[cube], cutoff=20.0)


def test_resolution_suffix_labels_both_sets() -> None:
    """Each cube says which resolution it is at, rather than one of them being
    the unlabelled default"""
    assert _resolution_suffix("pol", ResolutionModes.NATURAL) == "pol.natural"
    assert _resolution_suffix("pol", ResolutionModes.TOTAL) == "pol.total"
    assert _resolution_suffix(None, ResolutionModes.TOTAL) == "total"
