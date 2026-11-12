"""Tests around the beam cublet imaging flow"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import flint.prefect.flows.beam_cublet_image_pipeline as cublet_flow
from flint.cublet.islands import CubletRegion
from flint.ms import MS
from flint.prefect.flows.beam_cublet_image_pipeline import (
    BeamCubletOptions,
    flow_beam_cublet_image,
    get_parser,
)

STRATEGY = """
version: 0.2
defaults:
  wsclean:
    size: 1024
cubletimage:
  wsclean:
    channels_out: 4
    save_source_list: true
  masking:
    use_mac: true
cubletisland:
  wsclean:
    channels_out: 72
    auto_mask: null
"""


def test_get_parser():
    """Make sure the parser can actually be built"""
    _ = get_parser()


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


def _region(idx: int, peak: float) -> CubletRegion:
    return CubletRegion(
        idx=idx,
        centre=(10 * idx, 10 * idx),
        size=128,
        shift=("12h00m00s", "-30d00m00s"),
        super_weight=0.125,
        peak=peak,
    )


def _image_island(name_suffix: str, temp_dir: Path, **kwargs) -> SimpleNamespace:
    """Stands in for wsclean, with each island finding one component"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    source_list = temp_dir.parent / f"{name_suffix}-sources.txt"
    source_list.write_text("Format = Name, Type\ns0c0,POINT\n")
    return SimpleNamespace(
        image_set=SimpleNamespace(source_list=source_list),
        options=SimpleNamespace(flint_name_suffix=name_suffix),
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
        "task_find_cublet_regions": FakeTask(lambda **kwargs: regions),
        "task_image_island": FakeTask(_image_island),
        "task_concatenate_source_lists": FakeTask(
            cublet_flow.task_concatenate_source_lists.fn
        ),
        "task_remove_files_folders": FakeTask(lambda *args: None),
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


def test_flow_rounds(fake_tasks, tmp_path) -> None:
    """Islands are imaged in rounds, with all components subtracted in between"""
    options = _options(tmp_path, rounds=2, round_gain=0.8)
    flow_beam_cublet_image.fn(beam_cublet_options=options)

    work_dir = tmp_path / "cublet"
    island_calls = fake_tasks["task_image_island"].calls
    # Round 1 cleans to 1.0 Jy and round 2 to 0.2 Jy, which the faintest island only meets then
    assert [call["name_suffix"] for call in island_calls] == [
        "r1i00001",
        "r1i00002",
        "r2i00001",
        "r2i00002",
        "r2i00003",
    ]
    assert [call["threshold"] for call in island_calls] == pytest.approx(
        [1.0, 1.0, 0.2, 0.2, 0.2]
    )
    assert all(
        call["update_wsclean_options"]["channels_out"] == 72 for call in island_calls
    )

    # The brightest island of a round reorders the visibilities for the rest
    seed, linked = island_calls[0], island_calls[1]
    assert seed["temp_dir"] == work_dir / "round1"
    assert seed["save_reordered"]
    assert seed["seed_dir"] is None
    assert seed["wait_for"] is None
    assert linked["temp_dir"] == work_dir / "round1" / "island00002"
    assert linked["seed_dir"] == work_dir / "round1"
    assert linked["wait_for"] is not None

    # Round 1 images the data, and round 2 what is left of it
    assert island_calls[0]["ms"].column == "CORRECTED_DATA"
    assert island_calls[2]["ms"].column == "CUBLET_RESIDUAL"

    # Each round predicts every component so far into the original data
    predict_calls = fake_tasks["task_crystalball_to_ms"].calls
    assert [call["ms"].column for call in predict_calls] == ["CORRECTED_DATA"] * 2
    component_counts = [
        len(call["wsclean_source_list_path"].read_text().splitlines()) - 1
        for call in predict_calls
    ]
    assert component_counts == [2, 5]
    subtract_calls = fake_tasks["task_subtract_model_from_ms"].calls
    assert all(call["data_column"] == "CORRECTED_DATA" for call in subtract_calls)

    # The MFS image, then the image of what is left
    mfs_call, residual_call = fake_tasks["task_wsclean_imager"].calls
    assert mfs_call["update_wsclean_options"]["channels_out"] == 4
    assert residual_call["in_ms"].column == "CUBLET_RESIDUAL"
    assert residual_call["update_wsclean_options"]["niter"] == 0
    assert not residual_call["update_wsclean_options"]["save_source_list"]

    # MAC needs no RMS or background images
    assert fake_tasks["task_run_bane_and_aegean"].calls == []
    mask_call = fake_tasks["task_create_image_mask_model"].calls[0]
    assert mask_call["update_masking_options"] == {"use_mac": True}


def test_flow_without_reordered_reuse(fake_tasks, tmp_path) -> None:
    """Each island reorders for itself when the reordered visibilities are not shared"""
    options = _options(tmp_path, rounds=1, reuse_reordered=False)
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


def test_image_island_options(monkeypatch, tmp_path) -> None:
    """An island image is shifted, weighted as the full image and keeps the reordered files apart from its images"""
    captured: dict[str, Any] = {}

    def fake_imager(**kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(cublet_flow.task_wsclean_imager, "fn", fake_imager)

    ms = MS(path=tmp_path / "SB1234.RACS_0000+00.beam00.ms", column="DATA")
    seed_dir = tmp_path / "round1"
    seed_dir.mkdir()
    (seed_dir / f"{ms.path.name}-part0000-I.tmp").write_bytes(b"data")
    (seed_dir / f"{ms.path.name}-part0000-I-m.tmp").write_bytes(b"model")
    island_dir = seed_dir / "island00002"

    result = cublet_flow.task_image_island.fn(
        ms=ms,
        region=_region(idx=2, peak=3.0),
        wsclean_container=tmp_path / "wsclean.sif",
        update_wsclean_options={"channels_out": 72},
        threshold=0.5,
        name_suffix="r1i00002",
        temp_dir=island_dir,
        seed_dir=seed_dir,
    )
    assert result == "result"

    options = captured["update_wsclean_options"]
    assert options["channels_out"] == 72
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
    assert len(list(seed_dir.iterdir())) == 2
