"""Tests around the SPICE compression flow"""

from __future__ import annotations

from pathlib import Path

import pytest
from prefect import task
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint.options import SpiceFieldOptions
from flint.prefect.flows import spice_compression_pipeline


@task
def _fake_spice_fits(fits_path: Path, pixel_boxes, output_path: Path) -> Path:
    return output_path / fits_path.name


@task
def _fake_compress_cube(out_cube: Path, method, max_workers, quantize_level) -> Path:
    return Path(f"{out_cube}.fz")


@task
def _fake_spice_boxes(**kwargs) -> list:
    return []


def test_each_input_maps_to_the_file_it_became(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spicing deletes the inputs, so callers need to know what replaced each one"""
    for name, value in {
        "load_and_copy_strategy": lambda **kwargs: None,
        "common_beam_shape_from_cubes": lambda **kwargs: None,
        "shared_pixel_boxes": lambda **kwargs: None,
        "check_cubes_share_shape": lambda **kwargs: (1,),
        "task_get_spice_boxes": _fake_spice_boxes,
        "task_spice_fits": _fake_spice_fits,
        "task_compress_cube": _fake_compress_cube,
    }.items():
        monkeypatch.setattr(spice_compression_pipeline, name, value)

    cubes = [tmp_path / "q.fits", tmp_path / "u.fits"]
    weight_cubes = [tmp_path / "q.weight.fits"]
    output_path = tmp_path / "spice"

    with prefect_test_harness(), disable_run_logger():
        outputs = spice_compression_pipeline.process_spice_compression(
            spice_field_options=SpiceFieldOptions(
                cubes=cubes,
                weight_cubes=weight_cubes,
                catalogue=tmp_path / "components.fits",
                output_path=output_path,
            )
        )

    assert outputs == {
        cube: output_path / f"{cube.name}.fz" for cube in [*cubes, *weight_cubes]
    }
