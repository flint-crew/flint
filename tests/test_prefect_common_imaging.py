"""Tests around the prefect helpers for imaging and co-adding"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import flint.prefect.common.imaging as imaging_module
from flint.options import FieldOptions, FitsCubeOptions
from flint.prefect.common.imaging import create_convolve_linmos_cubes


def test_create_convolve_linmos_cubes_returns_plane_removals(monkeypatch) -> None:
    """The plane removals are handed back apart from the cubes, and are retried"""
    channel_groups = [[Path("beam00.ch0.fits"), Path("beam01.ch0.fits")]]
    convolved_channel_groups = [
        [Path("beam00.ch0.conv.fits"), Path("beam01.ch0.conv.fits")]
    ]

    transpose = MagicMock()
    transpose.submit.return_value.result.return_value = channel_groups
    convolve = MagicMock()
    convolve.map.return_value.result.return_value = convolved_channel_groups
    remove = MagicMock()
    remove_with_retries = remove.with_options.return_value
    remove_with_retries.submit.side_effect = [
        "remove_planes",
        "remove_convolved_planes",
    ]
    linmos_cubes = MagicMock()
    linmos_cubes.return_value.futures = ["image_cube", "weight_cube"]

    for name, value in {
        "task_split_beam_cube_into_planes": MagicMock(),
        "task_transpose_and_sort_channel_images": transpose,
        "task_get_common_beam_from_images": MagicMock(),
        "task_convolve_images": convolve,
        "task_remove_files_folders": remove,
        "linmos_channel_groups_to_cubes": linmos_cubes,
    }.items():
        monkeypatch.setattr(imaging_module, name, value)

    results = create_convolve_linmos_cubes(
        wsclean_results=[],
        field_options=FieldOptions(yandasoft_container=Path("yandasoft.sif")),
        fitscube_options=FitsCubeOptions(),
    )

    assert results.cubes == ["image_cube", "weight_cube"]
    assert results.cleanup == ["remove_planes", "remove_convolved_planes"]
    assert remove.with_options.call_args.kwargs["retries"] > 0
    remove_planes, remove_convolved_planes = remove_with_retries.submit.call_args_list
    assert remove_planes.args == tuple(channel_groups[0])
    assert remove_convolved_planes.args == tuple(convolved_channel_groups[0])
    assert remove_convolved_planes.kwargs["wait_for"] == ["image_cube", "weight_cube"]
