"""Tests around the prefect helpers for imaging and co-adding"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import flint.prefect.common.imaging as imaging_module
from flint.coadd.patchwork import PatchworkOptions
from flint.options import FieldOptions, FitsCubeOptions
from flint.prefect.common.imaging import (
    create_convol_linmos_images,
    create_convolve_linmos_cubes,
)


def test_task_remove_files_folders_is_retried() -> None:
    """A deletion that briefly fails on a shared filesystem is retried"""
    assert imaging_module.task_remove_files_folders.retries > 0


def test_create_convolve_linmos_cubes_returns_plane_removals(monkeypatch) -> None:
    """The plane removals are handed back apart from the cubes"""
    channel_groups = [[Path("beam00.ch0.fits"), Path("beam01.ch0.fits")]]
    convolved_channel_groups = [
        [Path("beam00.ch0.conv.fits"), Path("beam01.ch0.conv.fits")]
    ]

    transpose = MagicMock()
    transpose.submit.return_value.result.return_value = channel_groups
    convolve = MagicMock()
    convolve.map.return_value.result.return_value = convolved_channel_groups
    remove = MagicMock()
    remove.submit.side_effect = [
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
    remove_planes, remove_convolved_planes = remove.submit.call_args_list
    assert remove_planes.args == tuple(channel_groups[0])
    assert remove_convolved_planes.args == tuple(convolved_channel_groups[0])
    assert remove_convolved_planes.kwargs["wait_for"] == ["image_cube", "weight_cube"]


def test_create_convolve_linmos_cubes_with_patchwork(monkeypatch) -> None:
    """Given its options patchwork co-adds the cube, with the field summary to
    orient its beams, and no yandasoft container is asked for"""
    convolved_channel_groups = [[Path("beam00.ch0.conv.fits")]]
    convolve = MagicMock()
    convolve.map.return_value.result.return_value = convolved_channel_groups
    linmos_cubes = MagicMock()
    patchwork_cubes = MagicMock()
    patchwork_cubes.return_value = {"i": MagicMock(futures=["image_cube"])}

    for name, value in {
        "task_split_beam_cube_into_planes": MagicMock(),
        "task_transpose_and_sort_channel_images": MagicMock(),
        "task_get_common_beam_from_images": MagicMock(),
        "task_convolve_images": convolve,
        "task_remove_files_folders": MagicMock(),
        "linmos_channel_groups_to_cubes": linmos_cubes,
        "linmos_stokes_channel_groups_to_cubes": patchwork_cubes,
    }.items():
        monkeypatch.setattr(imaging_module, name, value)

    patchwork_options = PatchworkOptions()
    results = create_convolve_linmos_cubes(
        wsclean_results=[],
        field_options=FieldOptions(),
        fitscube_options=FitsCubeOptions(),
        field_summary="field_summary",  # type: ignore[arg-type]
        patchwork_options=patchwork_options,
    )

    assert results.cubes == ["image_cube"]
    linmos_cubes.assert_not_called()
    called_with = patchwork_cubes.call_args.kwargs
    assert called_with["stokes_channel_groups"] == {"i": convolved_channel_groups}
    assert called_with["patchwork_options"] == patchwork_options
    assert called_with["field_summary"] == "field_summary"


def test_create_convol_linmos_images_with_patchwork(monkeypatch) -> None:
    """Given its options patchwork co-adds the residual and restored images,
    trimmed as linmos trims them"""
    convolve = MagicMock()
    convolve.map.return_value = [[Path("beam00.conv.fits")], [Path("beam01.conv.fits")]]
    patchwork = MagicMock()
    patchwork.submit.side_effect = ["residual_mosaic", "image_mosaic"]
    linmos = MagicMock()

    for name, value in {
        "task_get_common_beam_from_results": MagicMock(),
        "task_convolve_image": convolve,
        "task_patchwork_images": patchwork,
        "task_linmos_images": linmos,
    }.items():
        monkeypatch.setattr(imaging_module, name, value)

    mosaics = create_convol_linmos_images(
        wsclean_results=[],
        field_options=FieldOptions(linmos_residuals=True, pb_cutoff=0.2),
        field_summary="field_summary",  # type: ignore[arg-type]
        additional_linmos_suffix_str="noselfcal",
        patchwork_options=PatchworkOptions(),
    )

    assert mosaics == ["residual_mosaic", "image_mosaic"]
    linmos.submit.assert_not_called()
    residual, image = (call.kwargs for call in patchwork.submit.call_args_list)
    assert residual["suffix_str"] == "noselfcal.optimal.residual"
    assert image["suffix_str"] == "noselfcal.optimal"
    assert image["image_list"] == [Path("beam00.conv.fits"), Path("beam01.conv.fits")]
    assert image["pb_cutoff"] == 0.2
    assert image["field_summary"] == "field_summary"
    assert image["trim"]
