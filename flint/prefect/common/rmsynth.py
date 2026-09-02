"""Prefect task wrappers for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from pathlib import Path

from prefect import unmapped

from flint.convol import BeamShape, convolve_plane_to_beam
from flint.logging import logger
from flint.options import FitsCubeOptions, RMCleanOptions, RMSynthOptions
from flint.prefect.caching import task
from flint.prefect.common.imaging import (
    task_combine_images_to_cube,
    task_remove_files_folders,
    task_split_cube_into_planes,
)
from flint.rmsynth import (
    FDFLabel,
    RMClean3DResults,
    RMSynth3DResults,
    run_rmclean_3d,
    run_rmsynth_3d,
    write_rm_products,
)

task_convolve_plane_to_beam = task(convolve_plane_to_beam)


def convolve_cubes_to_common_resolution(
    cubes: dict[str, Path],
    beam_shape: BeamShape,
    output_path: Path | None = None,
    beam_cutoff: float | None = None,
    convol_suffix: str = "conv",
) -> dict[str, Path]:
    """Bring a set of FITS cubes to the one resolution described by
    ``beam_shape``, writing new cubes and leaving the inputs as they are. This is
    the 'total' resolution mode of racs_tools: one beam for every channel of
    every cube. The polarisation stage instead leaves its cubes at a 'natural'
    resolution, one beam per channel, which is what they are archived at.

    Each cube is split into its per-channel planes, every plane of every cube is
    convolved as its own task, and the planes are then stacked back into a cube.
    A 3D convolution would instead work through one cube at a time on a single
    worker, and would pull each whole cube into memory to do it.

    Args:
        cubes (dict[str, Path]): The cubes to convolve, keyed however the caller wants the outputs keyed
        beam_shape (BeamShape): The resolution every channel is brought to
        output_path (Path | None, optional): Directory the new cubes are written into. Defaults to alongside each input cube.
        beam_cutoff (float | None, optional): Channels coarser than this, in arcsec, are blanked rather than convolved to. Defaults to no cutoff.
        convol_suffix (str, optional): The marker added to the name of a smoothed plane, and of the cube they are stacked into. Defaults to 'conv'.

    Returns:
        dict[str, Path]: The convolved cube for each key of ``cubes``
    """
    fitscube_options = FitsCubeOptions(
        # The weight cubes are not convolved, so the convolved image cubes have
        # to stay on the pixel and channel grid of the cubes they came from
        bounding_box=False,
        create_blanks=False,
        # Convolution has already put the pixel values on a new scale, so
        # leaving exact zeros be avoids reinterpreting them a second time
        invalidate_zeros=False,
        # rm-synth and the spice stage both read these cubes plane by plane, and
        # astropy cannot memmap a gzip file
        compress=False,
        remove_original_images=True,
    )

    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)

    cube_parent = {
        key: output_path if output_path is not None else cube.parent
        for key, cube in cubes.items()
    }
    # A plane's name carries only the flint name fields, so cubes that agree on
    # them (e.g. the same Stokes at differing stages) would clobber each other's
    # planes were they split into the same directory
    plane_paths = {
        key: cube_parent[key] / f"{cube.stem}.planes" for key, cube in cubes.items()
    }

    split_planes = {
        key: task_split_cube_into_planes.submit(cube=cube, output_path=plane_paths[key])
        for key, cube in cubes.items()
    }
    # Resolved here so the planes can be mapped over individually below
    planes_per_cube: dict[str, list[Path]] = {
        key: future.result() for key, future in split_planes.items()
    }

    # One task per plane rather than per cube, so every channel of every cube is
    # convolved across the cluster at once
    convolved_planes = task_convolve_plane_to_beam.map(
        plane=[plane for planes in planes_per_cube.values() for plane in planes],
        beam_shape=unmapped(beam_shape),
        cutoff=unmapped(beam_cutoff),
        convol_suffix=unmapped(convol_suffix),
    ).result()

    cube_futures = {}
    plane_idx = 0
    for key, planes in planes_per_cube.items():
        cube_futures[key] = task_combine_images_to_cube.submit(
            images=convolved_planes[plane_idx : plane_idx + len(planes)],
            prefix=str(cube_parent[key] / cubes[key].stem),
            mode=convol_suffix,
            fitscube_options=fitscube_options,
        )
        plane_idx += len(planes)
    assert plane_idx == len(convolved_planes), (
        f"Have {len(convolved_planes)} convolved planes across {plane_idx} channels"
    )

    convolved_cubes = {key: future.result() for key, future in cube_futures.items()}

    # The convolved planes are removed as each cube is assembled, leaving the
    # planes they were made from behind
    task_remove_files_folders.submit(*plane_paths.values()).result()

    return convolved_cubes


@task
def task_rmsynth(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    stokes_i_cube: Path | None = None,
    stokes_q_weight_cube: Path | None = None,
    stokes_u_weight_cube: Path | None = None,
    stokes_i_weight_cube: Path | None = None,
    stokes_q_noise_cube: Path | None = None,
    stokes_u_noise_cube: Path | None = None,
    stokes_i_noise_cube: Path | None = None,
) -> RMSynth3DResults:
    from prefect_dask import get_dask_client

    # Set as the default scheduler, not just borrowed: rm-lite's per-channel
    # noise estimates are eager `dask.compute` calls, so without a default
    # client they would read whole cubes on this one worker.
    with get_dask_client():
        return run_rmsynth_3d(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            stokes_q_weight_cube=stokes_q_weight_cube,
            stokes_u_weight_cube=stokes_u_weight_cube,
            rmsynth_options=rmsynth_options,
            stokes_i_cube=stokes_i_cube,
            stokes_i_weight_cube=stokes_i_weight_cube,
            stokes_q_noise_cube=stokes_q_noise_cube,
            stokes_u_noise_cube=stokes_u_noise_cube,
            stokes_i_noise_cube=stokes_i_noise_cube,
        )


@task
def task_rmclean(
    rm_synth_results: RMSynth3DResults, rmclean_options: RMCleanOptions
) -> RMClean3DResults:
    from prefect_dask import get_dask_client

    with get_dask_client():
        return run_rmclean_3d(
            rm_synth_results=rm_synth_results,
            rmclean_options=rmclean_options,
        )


@task
def task_write_rm_products(
    synth_results: RMSynth3DResults,
    clean_results: RMClean3DResults | None,
    stokes_q_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    peak_products: list[FDFLabel],
    output_prefix: Path,
) -> list[Path]:
    """Batch-compute and write the requested RM-synthesis/RM-CLEAN products"""
    from prefect_dask import get_dask_client

    with get_dask_client(set_as_default=False) as client:
        logger.info("Obtained the Client supporting the DaskTaskRunner.")
        return write_rm_products(
            synth_results=synth_results,
            clean_results=clean_results,
            stokes_q_cube=stokes_q_cube,
            rmsynth_options=rmsynth_options,
            rmclean_options=rmclean_options,
            cube_products=cube_products,
            moment_products=moment_products,
            peak_products=peak_products,
            output_prefix=output_prefix,
            dask_client=client,
        )
