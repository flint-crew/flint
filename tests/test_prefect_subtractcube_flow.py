"""Tests around the subtract cube imaging flow"""

from __future__ import annotations

import re
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from astropy.io import fits
from prefect import flow, task
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.coadd.linmos import LinmosResult
from flint.exceptions import FailedIntervalsError
from flint.options import SubtractFieldOptions
from flint.prefect.flows import subtract_cube_pipeline
from flint.prefect.flows.subtract_cube_pipeline import (
    get_parser,
    image_and_linmos_intervals,
    task_combine_all_linmos_images,
)
from tests.test_cube import _write_channel_image


def test_get_parser():
    """Make sure the parser can actually be built"""
    # This is a silly one but I was bitten by it not working, arrrrhh matey
    _ = get_parser()


def _linmos_results(tmp_path: Path) -> list[LinmosResult]:
    """Three channel mosaics, as linmos leaves them"""
    return [
        LinmosResult(
            cmd="linmos",
            parset=tmp_path / f"ch{channel}_parset.txt",
            image_fits=_write_channel_image(
                tmp_path
                / f"SB1234.RACS_0000-00.ch{channel:04d}-{channel + 1:04d}.linmos.fits",
                channel=channel,
                shape=(5, 7),
            ),
            weight_fits=tmp_path / f"ch{channel}.weight.fits",
        )
        for channel in range(3)
    ]


def test_combine_all_linmos_images_replaces_a_partial_cube(tmp_path: Path):
    """A cube left half written by a dead worker is replaced"""
    linmos_results = _linmos_results(tmp_path)
    partial_cube = tmp_path / "SB1234.RACS_0000-00.contsub.freq.linmos.cube.fits"
    partial_cube.write_bytes(b"half written")

    cube = task_combine_all_linmos_images.fn(
        linmos_commands=linmos_results,
        update_fits_cube_options={"remove_original_images": False},
    )

    assert cube == partial_cube
    assert fits.getdata(cube).squeeze().shape == (3, 5, 7)


def test_combine_all_linmos_images_missing_image_keeps_the_cube(tmp_path: Path):
    """With an image missing the cube already on disk is left alone"""
    linmos_results = _linmos_results(tmp_path)
    existing_cube = tmp_path / "SB1234.RACS_0000-00.contsub.freq.linmos.cube.fits"
    existing_cube.write_bytes(b"an earlier cube")
    missing = linmos_results[1].image_fits
    missing.unlink()

    with pytest.raises(FileNotFoundError, match=re.escape(missing.name)):
        task_combine_all_linmos_images.fn(linmos_commands=linmos_results)

    assert existing_cube.read_bytes() == b"an earlier cube"


def test_combine_all_linmos_images_keeps_images_until_compressed(
    tmp_path: Path, monkeypatch
):
    """A worker killed while compressing leaves the images for the rerun"""
    linmos_results = _linmos_results(tmp_path)
    images = [linmos_result.image_fits for linmos_result in linmos_results]
    combine = partial(
        task_combine_all_linmos_images.fn,
        linmos_commands=linmos_results,
        update_fits_cube_options={"compress": True, "compress_method": "tile-gzip"},
    )

    def killed(*args, **kwargs):
        raise RuntimeError("killed")

    with monkeypatch.context() as patch:
        patch.setattr(
            "flint.prefect.flows.subtract_cube_pipeline.compress_cube", killed
        )
        with pytest.raises(RuntimeError, match="killed"):
            combine()
    assert all(image.exists() for image in images)

    cube = combine()

    assert cube.name.endswith(".fits.fz")
    assert not any(image.exists() for image in images)


def image_intervals(
    monkeypatch,
    interval_count: int,
    failing: tuple[int, ...] = (),
    linmos_seconds: float = 0.0,
    **options,
) -> tuple[list[Any], dict[int, list[int]]]:
    """Run the interval loop with stand-in tasks, each interval's result being its index.

    Also returns, for each interval, which wsclean intervals had started by the
    time its linmos finished.
    """
    started: list[int] = []
    started_by_linmos: dict[int, list[int]] = {}

    @task
    def fake_wsclean(scan_range: tuple[int, int], **kwargs) -> int:
        started.append(scan_range[0])
        return scan_range[0]

    @task
    def fake_linmos(wsclean_results: int, **kwargs) -> int:
        time.sleep(linmos_seconds)
        started_by_linmos[wsclean_results] = list(started)
        if wsclean_results in failing:
            raise ValueError(f"interval {wsclean_results} broke")
        return wsclean_results

    monkeypatch.setattr(subtract_cube_pipeline, "task_map_all_wsclean", fake_wsclean)
    monkeypatch.setattr(
        subtract_cube_pipeline, "task_common_beam_convolve_linmos", fake_linmos
    )

    @flow
    def image_flow() -> list[Any]:
        return image_and_linmos_intervals(
            science_mss=(),
            subtract_field_options=SubtractFieldOptions(
                wsclean_container=Path("wsclean.sif"),
                yandasoft_container=Path("yandasoft.sif"),
                **options,
            ),
            update_wsclean_options={},
            range_name="scan_range",
            interval_count=interval_count,
        )

    with prefect_test_harness(), disable_run_logger():
        return image_flow(), started_by_linmos


def test_image_intervals_leaves_out_a_failed_interval(monkeypatch):
    """A failed interval is dropped and the rest keep their order"""
    results, _ = image_intervals(
        monkeypatch, interval_count=4, failing=(1,), max_failed_intervals=1
    )

    assert results == [0, 2, 3]


def test_image_intervals_too_many_failed(monkeypatch):
    """More failures than allowed fails the flow"""
    with pytest.raises(
        FailedIntervalsError, match="2 scan_range intervals failed.*interval 1 broke"
    ):
        image_intervals(
            monkeypatch, interval_count=4, failing=(0, 1), max_failed_intervals=1
        )


def test_image_intervals_all_failed(monkeypatch):
    """Nothing to cube fails the flow, even within the tolerance"""
    with pytest.raises(
        FailedIntervalsError, match="All 2 scan_range intervals.*interval 1 broke"
    ):
        image_intervals(
            monkeypatch, interval_count=2, failing=(0, 1), max_failed_intervals=10
        )


def test_image_intervals_stops_at_max_intervals(monkeypatch):
    """Only the first max_intervals are imaged"""
    results, _ = image_intervals(monkeypatch, interval_count=4, max_intervals=2)

    assert results == [0, 1]


def test_image_intervals_caps_those_in_flight(monkeypatch):
    """With two in flight, the third interval waits for the first to finish"""
    _, started_by_linmos = image_intervals(
        monkeypatch, interval_count=4, linmos_seconds=0.5, max_intervals_in_flight=2
    )

    assert 2 not in started_by_linmos[0]
    assert sorted(started_by_linmos[3]) == [0, 1, 2, 3]
