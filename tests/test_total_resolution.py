"""Tests around the 'total' resolution cubes the polarisation flow writes
alongside the natural ones: a single beam covering the whole band, which is what
RM-synthesis needs of its inputs.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from prefect import flow, task
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.coadd.linmos import LinmosOptions, LinmosResult
from flint.coadd.patchwork import PatchworkOptions
from flint.convol import (
    BeamShape,
    common_beam_from_beam_shapes,
    convolve_plane_to_beam,
    cubes_share_common_beam,
    get_common_beam,
)
from flint.cube import combine_images_to_cube, split_cube_into_planes
from flint.naming import ResolutionModes
from flint.options import FFTBANEOptions, FitsCubeOptions
from flint.prefect.common.imaging import (
    _resolution_suffix,
    convolve_channel_groups_to_natural_resolution,
    linmos_channel_groups_to_cubes,
    linmos_stokes_channel_groups_to_cubes,
    task_common_beam_from_beam_shapes,
)
from flint.summary import FieldSummary

# Reuse the synthetic helpers rather than keeping a second copy of them
from tests.test_convol import _write_cube_with_beam  # noqa: E402
from tests.test_cube import _write_channel_image  # noqa: E402
from tests.test_natural_resolution import _bmaj_arcsec, _channel_groups  # noqa: E402
from tests.test_patchwork_coadd import BORESIGHT, beam_images  # noqa: E402

STEM = "SB12345.BENCH_0000+00.ch0000-0019"


def _naturally_then_totally(
    stokes_channel_groups: dict[str, list[list[Path]]],
    cutoff: float | None = None,
) -> tuple[dict[str, list[list[Path]]], BeamShape]:
    """The order the polarisation flow runs them in: natural first, then the one
    beam covering the band solved over the result"""

    @flow
    def _run() -> tuple[dict[str, list[list[Path]]], BeamShape]:
        natural, channel_beam_shapes = convolve_channel_groups_to_natural_resolution(
            stokes_channel_groups=stokes_channel_groups, cutoff=cutoff
        )
        return natural, task_common_beam_from_beam_shapes.submit(
            beam_shapes=channel_beam_shapes, cutoff=cutoff
        ).result()

    with prefect_test_harness(), disable_run_logger():
        return _run()


def _cube_from_planes(
    planes: list[Path], prefix: Path, beam_shape: BeamShape | None = None
) -> Path:
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
        beam_shape=beam_shape,
    )


def _channel_beams(*bmaj_arcsec: float) -> list[BeamShape]:
    return [BeamShape(bmaj, bmaj * 0.8, 0.0) for bmaj in bmaj_arcsec]


def test_total_beam_covers_the_coarsest_channel() -> None:
    """One beam for the whole band has to cover every channel in it, or a channel
    it does not cover cannot be deconvolved to reach it"""
    beam = common_beam_from_beam_shapes(_channel_beams(10.0, 20.0, 15.0))

    assert beam.bmaj_arcsec >= 20.0


def test_total_beam_leaves_out_channels_beyond_the_cutoff() -> None:
    """A coarse channel is blanked when its plane is convolved, so it must not
    drag the total beam out to it, and a band of them has no total beam at all"""
    beam = common_beam_from_beam_shapes(_channel_beams(10.0, 40.0), cutoff=20.0)
    assert 10.0 <= beam.bmaj_arcsec < 40.0

    no_beam = common_beam_from_beam_shapes(_channel_beams(40.0, 50.0), cutoff=20.0)
    assert not np.isfinite(no_beam.bmaj_arcsec)


def test_total_beam_is_the_fixed_beam_when_one_is_given() -> None:
    """A fixed beam is used as it is, not rounded up as a solved one is"""
    beam = common_beam_from_beam_shapes(
        _channel_beams(10.0, 20.0), fixed_beam_shape=(25.05, 24.0, 3.0)
    )

    assert beam == BeamShape(25.05, 24.0, 3.0)


def test_total_beam_covers_a_channel_whose_first_image_is_blanked(
    tmp_path: Path,
) -> None:
    """An image beyond the cutoff is blanked and carries no beam. When it is the
    first image of its channel, the rest of that channel still has to be able to
    reach the total beam."""
    groups = _channel_groups(tmp_path, {"q": [[10.0, 10.5], [40.0, 18.0]]})

    natural, beam = _naturally_then_totally(groups, cutoff=20.0)

    blanked, kept = natural["q"][1]
    assert _bmaj_arcsec(blanked) == 0.0
    assert beam.bmaj_arcsec >= _bmaj_arcsec(kept) >= 18.0
    convolve_plane_to_beam(plane=kept, beam_shape=beam, cutoff=20.0)


def test_total_beam_covers_every_stokes(tmp_path: Path) -> None:
    """The beam is solved over the beam of each channel, which is only sound
    because the natural pass has already given every Stokes of a channel one
    resolution. Run in that order, the total beam still has to cover the coarsest
    Stokes."""
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


def test_a_band_entirely_beyond_the_cutoff_fails_clearly(tmp_path: Path) -> None:
    """With every channel beyond the cutoff there is no total beam to solve. The
    total cube has to say so, rather than fail on astropy refusing a NaN header
    value"""
    planes, total_beam = _planes_at_total_resolution(
        tmp_path, [40.0, 50.0], cutoff=20.0
    )
    assert not np.isfinite(total_beam.bmaj_arcsec)

    with pytest.raises(ValueError, match="not finite"):
        _cube_from_planes(
            planes=planes,
            prefix=tmp_path / f"{STEM}.q.total",
            beam_shape=total_beam,
        )


def test_resolution_suffix_labels_both_sets() -> None:
    """Each cube says which resolution it is at, rather than one of them being
    the unlabelled default"""
    assert _resolution_suffix("pol", ResolutionModes.NATURAL) == "pol.natural"
    assert _resolution_suffix("pol", ResolutionModes.TOTAL) == "pol.total"
    assert _resolution_suffix(None, ResolutionModes.TOTAL) == "total"


@dataclass(eq=False)
class _Submitted:
    """Stands in for the future of one faked ``.submit``, compared by identity"""

    task: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


def _fake_submits(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[_Submitted]:
    """Fake the ``.submit`` of each named task, listing what gets submitted"""
    submitted: list[_Submitted] = []
    for name in names:

        def fake_submit(*args: Any, _name: str = name, **kwargs: Any) -> _Submitted:
            future = _Submitted(task=_name, args=args, kwargs=kwargs)
            submitted.append(future)
            return future

        monkeypatch.setattr(f"flint.prefect.common.imaging.{name}.submit", fake_submit)
    return submitted


def test_image_cubes_wait_for_every_reader_of_their_planes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image cube deletes the planes it stacks, so it may not be built until
    every other task reading those planes - the total convolution and BANE - has
    finished with them"""
    submitted = _fake_submits(
        monkeypatch,
        "task_linmos_images",
        "task_getattr",
        "task_bane_fits_image",
        "task_convolve_plane_to_beam",
        "task_create_name_from_common_fields",
        "task_combine_images_to_cube",
    )

    cubes = linmos_channel_groups_to_cubes(
        channel_groups=[[Path("ch0.beam00.fits")], [Path("ch1.beam00.fits")]],
        container=Path("yandasoft.sif"),
        linmos_options=LinmosOptions(),
        fitscube_options=FitsCubeOptions(bounding_box=False),
        fft_bane_options=FFTBANEOptions(),
        total_beam_shape=BeamShape(bmaj_arcsec=20.0, bmin_arcsec=20.0, bpa_deg=0.0),
    )

    total_planes = {s for s in submitted if s.task == "task_convolve_plane_to_beam"}
    bane = [s for s in submitted if s.task == "task_bane_fits_image"]
    natural_bane = {s for s in bane if s.kwargs["image"] not in total_planes}
    total_bane = {s for s in bane if s.kwargs["image"] in total_planes}
    assert len(total_planes) == len(natural_bane) == len(total_bane) == 2

    assert set(cubes.image.kwargs["wait_for"]) == natural_bane | total_planes
    assert (
        cubes.total_image is not None
        and set(cubes.total_image.kwargs["wait_for"]) == total_bane
    )
    # The other cubes are the only readers of their own planes
    for cube in (cubes.weight, cubes.bkg, cubes.rms, cubes.total_bkg, cubes.total_rms):
        assert cube is not None
        assert cube.kwargs["wait_for"] is None


def _submitted_for_two_stokes(
    monkeypatch: pytest.MonkeyPatch, bounding_box: bool
) -> list[_Submitted]:
    """Everything submitted to cube a single channel of Stokes I and Q"""
    submitted = _fake_submits(
        monkeypatch,
        "task_linmos_images",
        "task_getattr",
        "task_create_name_from_common_fields",
        "task_get_common_bounding_box",
        "task_extract_common_bounding_box",
        "task_combine_images_to_cube",
    )
    linmos_stokes_channel_groups_to_cubes(
        stokes_channel_groups={
            "i": [[Path("i.ch0.beam00.fits")]],
            "q": [[Path("q.ch0.beam00.fits")]],
        },
        container=Path("yandasoft.sif"),
        linmos_options=LinmosOptions(),
        fitscube_options=FitsCubeOptions(bounding_box=bounding_box),
    )
    return submitted


def test_stokes_cubes_are_trimmed_to_one_merged_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Stokes has its box found over its own planes, and the merge of those
    is the one box every cube of every Stokes is trimmed to"""
    submitted = _submitted_for_two_stokes(monkeypatch, bounding_box=True)

    boxes = [s for s in submitted if s.task == "task_get_common_bounding_box"]
    (merged,) = [s for s in submitted if s.task == "task_extract_common_bounding_box"]
    cubes = [s for s in submitted if s.task == "task_combine_images_to_cube"]
    image_cubes = [cube for cube in cubes if cube.kwargs["mode"] == "image"]

    assert len(cubes) == 4, "an image and a weight cube for each Stokes"
    assert [box.kwargs["file_list"] for box in boxes] == [
        cube.kwargs["images"] for cube in image_cubes
    ]
    assert merged.kwargs["bounding_boxes"] == boxes
    assert all(cube.kwargs["bounding_box"] is merged for cube in cubes)


def test_no_box_is_found_when_stokes_cubes_are_not_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With trimming off the planes are never read for a box"""
    submitted = _submitted_for_two_stokes(monkeypatch, bounding_box=False)

    assert not [s for s in submitted if "bounding_box" in s.task]
    cubes = [s for s in submitted if s.task == "task_combine_images_to_cube"]
    assert len(cubes) == 4
    assert all(cube.kwargs["bounding_box"] is False for cube in cubes)


@task
def _linmos_as_first_image(image_list: list[Path], **_: Any) -> LinmosResult:
    """Stands in for linmos: the mosaic and its weight are the first beam image"""
    beam_image = image_list[0]
    stem = beam_image.name.replace(".beam00", "").removesuffix(".image.fits")
    mosaic = beam_image.with_name(f"{stem}.pol.linmos.fits")
    weight = beam_image.with_name(f"{stem}.pol.weight.fits")
    shutil.copy(beam_image, mosaic)
    shutil.copy(beam_image, weight)
    return LinmosResult(
        cmd="linmos",
        parset=beam_image.with_name(f"{stem}.parset"),
        image_fits=mosaic,
        weight_fits=weight,
    )


def test_every_stokes_is_cubed_on_one_pixel_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stokes Q mosaics whose valid pixels stop short of those of Stokes I still
    give cubes of the shape of the Stokes I ones, or rm-synth cannot line them up"""
    stokes_channel_groups = {
        stokes: [
            [
                _write_channel_image(
                    tmp_path
                    / f"SB1234.RACS_0000-00.beam00.round3.{stokes}.ch{channel:04d}-{channel + 1:04d}.image.fits",
                    channel=channel,
                    shape=(12, 14),
                    nan_border=nan_border,
                )
            ]
            for channel in range(3)
        ]
        for stokes, nan_border in {"i": 1, "q": 2}.items()
    }
    monkeypatch.setattr(
        "flint.prefect.common.imaging.task_linmos_images", _linmos_as_first_image
    )

    @flow
    def _cube() -> dict[str, list[Path]]:
        stokes_cubes = linmos_stokes_channel_groups_to_cubes(
            stokes_channel_groups=stokes_channel_groups,
            container=Path("yandasoft.sif"),
            linmos_options=LinmosOptions(),
            fitscube_options=FitsCubeOptions(invalidate_zeros=False),
            suffix_str="pol",
        )
        return {
            stokes: [cube.result() for cube in cubes.futures]
            for stokes, cubes in stokes_cubes.items()
        }

    with prefect_test_harness(), disable_run_logger():
        stokes_cubes = _cube()

    shapes = {
        fits.getdata(cube).squeeze().shape
        for cubes in stokes_cubes.values()
        for cube in cubes
    }
    assert shapes == {(3, 10, 12)}, "the valid pixels of Stokes I"

    q_image = fits.getdata(stokes_cubes["q"][0]).squeeze()
    assert np.all(np.isfinite(q_image[:, 1:-1, 1:-1]))
    q_image[:, 1:-1, 1:-1] = np.nan
    assert np.all(np.isnan(q_image)), "Stokes Q is padded out to the Stokes I box"


def test_patchwork_mosaics_every_stokes_of_a_channel_in_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q and U are only rotated when mosaicked together, so with patchwork a
    channel's Stokes share one task, and linmos is never called"""
    submitted = _fake_submits(
        monkeypatch,
        "task_linmos_images",
        "task_patchwork_stokes_images",
        "task_getitem",
        "task_getattr",
        "task_create_name_from_common_fields",
        "task_combine_images_to_cube",
    )

    linmos_stokes_channel_groups_to_cubes(
        stokes_channel_groups={
            "q": [[Path("q.ch0.beam00.fits")], [Path("q.ch1.beam00.fits")]],
            "u": [[Path("u.ch0.beam00.fits")], [Path("u.ch1.beam00.fits")]],
        },
        container=None,
        linmos_options=LinmosOptions(cutoff=0.2),
        fitscube_options=FitsCubeOptions(bounding_box=False),
        patchwork_options=PatchworkOptions(),
    )

    assert not [s for s in submitted if s.task == "task_linmos_images"]
    mosaics = [s for s in submitted if s.task == "task_patchwork_stokes_images"]
    assert [mosaic.kwargs["stokes_images"] for mosaic in mosaics] == [
        {"q": [Path("q.ch0.beam00.fits")], "u": [Path("u.ch0.beam00.fits")]},
        {"q": [Path("q.ch1.beam00.fits")], "u": [Path("u.ch1.beam00.fits")]},
    ]
    assert all(mosaic.kwargs["pb_cutoff"] == 0.2 for mosaic in mosaics)

    cubes = [s for s in submitted if s.task == "task_combine_images_to_cube"]
    assert len(cubes) == 4, "an image and a weight cube for each Stokes"
    for cube in cubes:
        # Each plane is an attribute of one Stokes looked up in a channel's mosaics
        lookups = [plane.args[0] for plane in cube.kwargs["images"]]
        assert [lookup.args[0] for lookup in lookups] == mosaics
        assert len({lookup.args[1] for lookup in lookups}) == 1


def test_patchwork_cubes_every_stokes_on_one_pixel_grid(tmp_path: Path) -> None:
    """Real patchwork mosaics of each channel stack into cubes of one shape for
    every Stokes, with nothing standing in for the mosaicker"""
    stokes_channel_groups = {
        stokes: [
            beam_images(
                tmp_path, stokes, channel=channel, frequency_hz=9.4e8 + channel * 1e7
            )
            for channel in range(2)
        ]
        for stokes in ("i", "q", "u")
    }

    @flow
    def _cube() -> dict[str, list[Path]]:
        stokes_cubes = linmos_stokes_channel_groups_to_cubes(
            stokes_channel_groups=stokes_channel_groups,
            container=None,
            linmos_options=LinmosOptions(),
            fitscube_options=FitsCubeOptions(),
            field_summary=FieldSummary(
                sbid="1234",
                cal_sbid="None",
                field_name="RACS_0000-00",
                boresight=BORESIGHT,
            ),
            suffix_str="pol",
            patchwork_options=PatchworkOptions(backend="numpy"),
        )
        return {
            stokes: [cube.result() for cube in cubes.futures]
            for stokes, cubes in stokes_cubes.items()
        }

    with prefect_test_harness(), disable_run_logger():
        stokes_cubes = _cube()

    shapes = {
        fits.getdata(cube).squeeze().shape
        for cubes in stokes_cubes.values()
        for cube in cubes
    }
    assert len(shapes) == 1
    assert shapes.pop()[0] == 2, "a plane for each channel"
    for stokes, cubes in stokes_cubes.items():
        assert len(cubes) == 2, "an image and a weight cube"
        assert np.any(np.isfinite(fits.getdata(cubes[0]))), stokes
