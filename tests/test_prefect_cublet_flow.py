"""Tests around the beam cublet imaging flow"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from capn_crunch import create_options_from_parser
from eye_patch.naming import FITSMaskNames

import flint.prefect.flows.beam_cublet_image_pipeline as cublet_flow
from flint.cublet.islands import CubletRegion
from flint.ms import MS
from flint.predict.crystalball import CrystalBallOptions
from flint.prefect.flows.beam_cublet_image_pipeline import (
    BeamCubletOptions,
    IslandImage,
    flow_beam_cublet_image,
    get_parser,
)

STRATEGY = """
version: 0.2
defaults:
  wsclean:
    size: 1024
cubletimage:
  1:
    wsclean:
      channels_out: 4
      save_source_list: true
    masking:
      use_mac: true
  2:
    wsclean:
      channels_out: 6
    masking:
      use_mac: true
      seed_clip: 1.2
cubletisland:
  1:
    wsclean:
      channels_out: 72
      auto_mask: null
"""


def test_get_parser():
    """Make sure the parser can actually be built"""
    _ = get_parser()


def test_parser_crystalball_chunks() -> None:
    """The chunks crystalball predicts in can be set"""
    args = get_parser().parse_args(
        ["a.ms", "strategy.yaml", "wsclean.sif", "cluster.yaml"]
        + ["--row-chunks", "5000", "--model-chunks", "100"]
    )
    crystalball_options = create_options_from_parser(
        parser_namespace=args, options_class=CrystalBallOptions
    )
    assert crystalball_options.row_chunks == 5000
    assert crystalball_options.model_chunks == 100


class FakeFuture:
    def __init__(self, value: Any) -> None:
        self.value = value

    def result(self) -> Any:
        return self.value


def _resolve(item: Any) -> Any:
    """Unwrap futures as prefect does, including those in a list"""
    if isinstance(item, FakeFuture):
        return item.value
    if isinstance(item, list):
        return [_resolve(value) for value in item]
    return item


class FakeTask:
    """Runs a function when submitted, and remembers what it was called with"""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[dict[str, Any]] = []

    def submit(self, *args, wait_for=None, **kwargs) -> FakeFuture:
        kwargs = {key: _resolve(value) for key, value in kwargs.items()}
        self.calls.append({**kwargs, "args": args, "wait_for": wait_for})
        return FakeFuture(self.fn(*map(_resolve, args), **kwargs))


def _region(idx: int, peak: float, **kwargs) -> CubletRegion:
    return CubletRegion(
        idx=idx,
        centre=(10 * idx, 10 * idx),
        size=128,
        shift=("12h00m00s", "-30d00m00s"),
        super_weight=0.125,
        peak=peak,
        **kwargs,
    )


def _image_island(
    region: CubletRegion, threshold: float, name_suffix: str, temp_dir: Path, **kwargs
) -> IslandImage:
    """Stands in for wsclean. Each island gives one component, and is cleaned to the threshold"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    source_list = temp_dir.parent / f"{name_suffix}-sources.txt"
    source_list.write_text("Format = Name, Type\ns0c0,POINT\n")
    return IslandImage(
        region=region.with_options(peak=threshold),
        tag=name_suffix,
        source_list=source_list,
    )


@pytest.fixture
def fake_tasks(monkeypatch) -> dict[str, FakeTask]:
    regions = [
        _region(idx=1, peak=5.0),
        _region(idx=2, peak=3.0),
        _region(idx=3, peak=0.4),
    ]
    tasks = {
        "task_wsclean_imager": FakeTask(lambda **kwargs: "image"),
        "task_run_bane_and_aegean": FakeTask(lambda **kwargs: "aegean"),
        "task_create_image_mask_model": FakeTask(lambda **kwargs: "mask"),
        "task_union_masks": FakeTask(lambda **kwargs: "union"),
        "task_find_cublet_regions": FakeTask(lambda **kwargs: regions),
        "task_image_island": FakeTask(_image_island),
        "task_concatenate_source_lists": FakeTask(
            cublet_flow.task_concatenate_source_lists.fn
        ),
        "task_remove_files_folders": FakeTask(lambda *args: None),
        "task_restore_source_list": FakeTask(lambda **kwargs: "restored"),
        "task_crystalball_to_ms": FakeTask(lambda ms, **kwargs: ms),
        "task_subtract_model_from_ms": FakeTask(
            lambda ms, output_column, **kwargs: ms.with_options(column=output_column)
        ),
    }
    for name, fake_task in tasks.items():
        monkeypatch.setattr(cublet_flow, name, fake_task)
    return tasks


def _options(tmp_path: Path, **kwargs) -> BeamCubletOptions:
    strategy = tmp_path / "strategy.yaml"
    strategy.write_text(STRATEGY)
    return BeamCubletOptions(
        ms_path=tmp_path / "SB1234.RACS_0000+00.beam00.ms",
        strategy=strategy,
        wsclean_container=tmp_path / "wsclean.sif",
        cluster_config=tmp_path / "cluster.yaml",
        **kwargs,
    )


def _component_count(source_list: Path) -> int:
    return len(source_list.read_text().splitlines()) - 1


def test_flow_rounds(fake_tasks, tmp_path) -> None:
    """Islands are imaged in rounds, with their new components subtracted in between"""
    options = _options(tmp_path, rounds=2, round_gain=0.8)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    work_dir = tmp_path / "cublet"
    island_calls = fake_tasks["task_image_island"].calls
    # Round 1 cleans to 1.0 Jy and round 2 to 0.2 Jy, which the faintest island only meets then
    assert [call["name_suffix"] for call in island_calls] == [
        "c1r1i00001",
        "c1r1i00002",
        "c1r2i00001",
        "c1r2i00002",
        "c1r2i00003",
    ]
    assert [call["threshold"] for call in island_calls] == pytest.approx(
        [1.0, 1.0, 0.2, 0.2, 0.2]
    )
    assert all(
        call["update_wsclean_options"]["channels_out"] == 72 for call in island_calls
    )

    # The brightest island of a round reorders the visibilities for the rest
    seed, linked = island_calls[0], island_calls[1]
    assert seed["temp_dir"] == work_dir / "cycle1" / "round1"
    assert seed["save_reordered"]
    assert seed["seed_dir"] is None
    assert seed["wait_for"] is None
    assert linked["temp_dir"] == work_dir / "cycle1" / "round1" / "island00002"
    assert linked["seed_dir"] == work_dir / "cycle1" / "round1"
    assert linked["wait_for"] is not None

    # Each round images, predicts into and subtracts from what the last round left
    columns = ["CORRECTED_DATA", "CUBLET_RESIDUAL", "CUBLET_RESIDUAL_ALT"]
    assert island_calls[0]["ms"].column == columns[0]
    assert island_calls[2]["ms"].column == columns[1]
    predict_calls = fake_tasks["task_crystalball_to_ms"].calls
    assert [call["ms"].column for call in predict_calls] == columns[:2]
    assert predict_calls[0]["crystalball_options"] == CrystalBallOptions()
    subtract_calls = fake_tasks["task_subtract_model_from_ms"].calls
    assert [call["ms"].column for call in subtract_calls] == columns[:2]
    assert [call["output_column"] for call in subtract_calls] == columns[1:]

    # Only the components of a round are predicted, and all are kept on record
    assert [
        _component_count(call["wsclean_source_list_path"]) for call in predict_calls
    ] == [2, 3]
    all_components = work_dir / "SB1234.RACS_0000+00.beam00.to-c1r2-sources.txt"
    assert _component_count(all_components) == 5

    # The MFS image, then the image of what is left
    mfs_call, residual_call = fake_tasks["task_wsclean_imager"].calls
    assert mfs_call["update_wsclean_options"]["channels_out"] == 4
    assert residual_call["in_ms"].column == columns[2]
    assert residual_call["update_wsclean_options"]["niter"] == 0
    assert not residual_call["update_wsclean_options"]["save_source_list"]

    # All components found are restored onto the image of what is left
    (restore_call,) = fake_tasks["task_restore_source_list"].calls
    assert restore_call["wsclean_result"] == "image"
    assert restore_call["source_list"] == all_components

    # MAC needs no RMS or background images
    assert fake_tasks["task_run_bane_and_aegean"].calls == []
    mask_call = fake_tasks["task_create_image_mask_model"].calls[0]
    assert mask_call["update_masking_options"] == {"use_mac": True}


def test_flow_skips_finished_islands(fake_tasks, tmp_path) -> None:
    """An island with nothing left above the level of a round is not imaged in it"""

    def image_island(region, **kwargs) -> IslandImage:
        island_image = _image_island(region=region, **kwargs)
        left = 0.0 if region.idx == 1 else island_image.region.peak
        return island_image.with_options(region=region.with_options(peak=left))

    fake_tasks["task_image_island"].fn = image_island
    options = _options(tmp_path, rounds=2, round_gain=0.8)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    island_calls = fake_tasks["task_image_island"].calls
    assert [call["name_suffix"] for call in island_calls] == [
        "c1r1i00001",
        "c1r1i00002",
        "c1r2i00002",
        "c1r2i00003",
    ]
    # The next brightest island reorders the visibilities in its place
    assert island_calls[2]["save_reordered"]


def test_flow_without_reordered_reuse(fake_tasks, tmp_path) -> None:
    """Each island reorders for itself when the reordered visibilities are not shared"""
    options = _options(tmp_path, rounds=1, round_gain=0.8, no_reuse_reordered=True)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    island_calls = fake_tasks["task_image_island"].calls
    assert len(island_calls) == 2
    for call in island_calls:
        assert call["seed_dir"] is None
        assert not call["save_reordered"]
        assert call["wait_for"] is None
    assert len({call["temp_dir"] for call in island_calls}) == 2


def test_flow_no_islands(fake_tasks, tmp_path) -> None:
    """Without islands nothing is imaged past the MFS image"""
    fake_tasks["task_find_cublet_regions"].fn = lambda **kwargs: []
    flow_beam_cublet_image.fn(beam_cublet_options=_options(tmp_path))

    assert fake_tasks["task_image_island"].calls == []
    assert len(fake_tasks["task_wsclean_imager"].calls) == 1
    assert fake_tasks["task_restore_source_list"].calls == []


def test_image_island(monkeypatch, tmp_path) -> None:
    """An island image is shifted, weighted as the full image and keeps the reordered files apart from its images"""
    mask_fits = tmp_path / "island.mask.fits"
    residual_fits = tmp_path / "island.residual.fits"
    mask = np.zeros((128, 128), dtype=np.float32)
    mask[60:70, 60:70] = 1
    residual = np.full((1, 1, 128, 128), 9.0, dtype=np.float32)
    residual[..., 60:70, 60:70] = 0.25
    fits.writeto(mask_fits, data=mask)
    fits.writeto(residual_fits, data=residual)
    source_list = tmp_path / "island-sources.txt"
    source_list.write_text("components")
    image_fits = tmp_path / "island-image.fits"

    captured: dict[str, Any] = {}

    def fake_imager(**kwargs):
        captured.update(kwargs)
        image_fits.touch()
        return SimpleNamespace(
            image_set=SimpleNamespace(
                image=[image_fits],
                psf=None,
                dirty=None,
                model=None,
                residual=[residual_fits],
                source_list=source_list,
            )
        )

    monkeypatch.setattr(cublet_flow.task_wsclean_imager, "fn", fake_imager)

    ms = MS(path=tmp_path / "SB1234.RACS_0000+00.beam00.ms", column="DATA")
    seed_dir = tmp_path / "round1"
    seed_dir.mkdir()
    (seed_dir / f"{ms.path.name}-part0000-I.tmp").write_bytes(b"data")
    island_dir = seed_dir / "island00002"

    island_image = cublet_flow.task_image_island.fn(
        ms=ms,
        region=_region(idx=2, peak=3.0, fits_mask=FITSMaskNames(mask_fits=mask_fits)),
        wsclean_container=tmp_path / "wsclean.sif",
        update_wsclean_options={"channels_out": 72},
        threshold=0.5,
        name_suffix="r1i00002",
        temp_dir=island_dir,
        seed_dir=seed_dir,
    )
    # What is left of the island is the brightest residual inside its mask
    assert island_image.region.peak == 0.25
    assert island_image.tag == "r1i00002"
    assert island_image.source_list == source_list

    # Only the components of the island are kept
    assert not image_fits.exists()
    assert not residual_fits.exists()
    assert source_list.exists()

    options = captured["update_wsclean_options"]
    assert options["channels_out"] == 72
    assert options["mgain"] == 1.0
    assert options["size"] == 128
    assert options["shift"] == ("12h00m00s", "-30d00m00s")
    assert options["super_weight"] == 0.125
    assert options["threshold"] == 0.5
    assert options["save_source_list"]
    assert options["no_update_model_required"]
    assert options["reuse_reordered"]
    assert options["save_reordered"]
    assert options["temp_dir"] == island_dir
    assert options["flint_hold_dir"] == ms.path.parent

    # The linked files are removed with the island, and the seed is left alone
    assert not island_dir.exists()
    assert len(list(seed_dir.iterdir())) == 1

    # The strategy may still ask wsclean for major cycles of its own
    cublet_flow.task_image_island.fn(
        ms=ms,
        region=_region(idx=2, peak=3.0),
        wsclean_container=tmp_path / "wsclean.sif",
        update_wsclean_options={"mgain": 0.8},
        threshold=0.5,
        name_suffix="r1i00002",
        temp_dir=island_dir,
        keep_images=True,
    )
    assert captured["update_wsclean_options"]["mgain"] == 0.8
    assert image_fits.exists()


def test_flow_crystalball_options(fake_tasks, tmp_path) -> None:
    """The crystalball options given are those every round predicts with"""
    crystalball_options = CrystalBallOptions(row_chunks=5000, model_chunks=100)
    flow_beam_cublet_image.fn(
        beam_cublet_options=_options(tmp_path, rounds=2, round_gain=0.8),
        crystalball_options=crystalball_options,
    )
    predict_calls = fake_tasks["task_crystalball_to_ms"].calls
    assert [call["crystalball_options"] for call in predict_calls] == [
        crystalball_options,
        crystalball_options,
    ]


def test_flow_cycles(fake_tasks, tmp_path) -> None:
    """Later cycles image what is left, and add their islands to those already known"""
    options = _options(tmp_path, cycles=3, rounds=2, later_rounds=1, round_gain=0.8)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    work_dir = tmp_path / "cublet"
    columns = ["CORRECTED_DATA", "CUBLET_RESIDUAL", "CUBLET_RESIDUAL_ALT"]

    # Two rounds in the first cycle, then one in each later cycle
    island_calls = fake_tasks["task_image_island"].calls
    rounds = [call["name_suffix"][:4] for call in island_calls]
    assert sorted(set(rounds)) == ["c1r1", "c1r2", "c2r1", "c3r1"]
    assert island_calls[-1]["temp_dir"].parent == work_dir / "cycle3" / "round1"

    # Each subtraction writes to the column the last did not
    subtract_calls = fake_tasks["task_subtract_model_from_ms"].calls
    assert [call["output_column"] for call in subtract_calls] == [
        columns[1],
        columns[2],
        columns[1],
        columns[2],
    ]

    # An image for each cycle, of what the cycle before left, then one of what is left
    wsclean_calls = fake_tasks["task_wsclean_imager"].calls
    assert [call["in_ms"].column for call in wsclean_calls] == [
        columns[0],
        columns[2],
        columns[1],
        columns[2],
    ]
    suffixes = [
        call["update_wsclean_options"].get("flint_name_suffix")
        for call in wsclean_calls
    ]
    assert suffixes == [None, "cycle2", "cycle3", "cubletresidual"]
    # The image of what is left needs a beam to be restored with
    assert wsclean_calls[-1]["update_wsclean_options"]["make_psf"]
    # A cycle past the last in the strategy uses the last
    channels = [
        call["update_wsclean_options"]["channels_out"] for call in wsclean_calls
    ]
    assert channels == [4, 6, 6, 6]
    mask_calls = fake_tasks["task_create_image_mask_model"].calls
    assert [call["update_masking_options"].get("seed_clip") for call in mask_calls] == [
        None,
        1.2,
        1.2,
    ]
    assert all(
        call["update_wsclean_options"]["channels_out"] == 72 for call in island_calls
    )

    # The mask of a cycle is joined with that of all the cycles before it
    union_calls = fake_tasks["task_union_masks"].calls
    assert [call["earlier_mask_names"] for call in union_calls] == ["mask", "union"]
    assert union_calls[0]["output_dir"] == work_dir / "cycle2"
    region_calls = fake_tasks["task_find_cublet_regions"].calls
    assert [call["fits_mask"] for call in region_calls] == ["mask", "union", "union"]
    assert region_calls[1]["output_dir"] == work_dir / "cycle2" / "masks"

    # The components of every cycle are kept on record together
    all_components = work_dir / "SB1234.RACS_0000+00.beam00.to-c3r1-sources.txt"
    assert _component_count(all_components) == len(island_calls)


def test_flow_cycles_stop(fake_tasks, tmp_path) -> None:
    """A cycle that finds no islands is the last"""
    regions = fake_tasks["task_find_cublet_regions"].fn()
    found = iter([regions, []])
    fake_tasks["task_find_cublet_regions"].fn = lambda **kwargs: next(found)
    options = _options(tmp_path, cycles=4, rounds=1, round_gain=0.8)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    # The images of two cycles, then the image of what is left
    assert len(fake_tasks["task_wsclean_imager"].calls) == 3
    island_calls = fake_tasks["task_image_island"].calls
    assert {call["name_suffix"][:4] for call in island_calls} == {"c1r1"}


def test_strategy_keyed_by_cycle(tmp_path) -> None:
    """The cublet operations are keyed by cycle, as selfcal is by round"""
    from flint.configuration import load_strategy_yaml

    strategy = tmp_path / "strategy.yaml"
    strategy.write_text(STRATEGY)
    load_strategy_yaml(input_yaml=strategy, verify=True)

    flat = STRATEGY.replace("  1:\n    wsclean:\n      channels_out: 72", "  wsclean:")
    assert flat != STRATEGY
    strategy.write_text(flat)
    with pytest.raises(ValueError):
        load_strategy_yaml(input_yaml=strategy, verify=True)


def test_task_restore_source_list(monkeypatch, tmp_path) -> None:
    """The MFS image of what is left is restored, under a name of its own"""
    calls = []
    monkeypatch.setattr(
        cublet_flow, "restore_source_list", lambda **kwargs: calls.append(kwargs)
    )
    images = [
        tmp_path / "SB1234.cubletresidual.i.image.cube.fits",
        tmp_path / "SB1234.cubletresidual.i.MFS.image.fits",
    ]
    cublet_flow.task_restore_source_list.fn(
        wsclean_result=SimpleNamespace(image_set=SimpleNamespace(image=images)),
        source_list=tmp_path / "all-sources.txt",
        wsclean_container=tmp_path / "wsclean.sif",
    )
    assert calls == [
        {
            "residual_image": images[1],
            "source_list": tmp_path / "all-sources.txt",
            "output_image": tmp_path / "SB1234.cubletrestored.i.MFS.image.fits",
            "container": tmp_path / "wsclean.sif",
        }
    ]
