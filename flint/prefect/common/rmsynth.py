"""Prefect task wrappers for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from prefect import unmapped
from prefect.futures import PrefectFuture

from flint.bane import BANEMaps
from flint.convol import BeamShape, convolve_plane_to_beam
from flint.logging import logger
from flint.options import (
    CubesForRMSynth,
    ErrorCubesForRMSynth,
    FFTBANEOptions,
    FitsCubeOptions,
    RMCleanOptions,
    RMSynthOptions,
)
from flint.prefect.caching import task
from flint.prefect.common.imaging import (
    task_bane_fits_image,
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


class CommonResolutionCubes(NamedTuple):
    """The cubes ``convolve_cubes_to_common_resolution`` writes, keyed by Stokes"""

    cubes: dict[str, Path]
    """The cube to use per Stokes: convolved, or the input unchanged"""
    convolved: bool = False
    """False when the inputs already shared a resolution and nothing new was written"""
    bkg_cubes: dict[str, Path] = {}
    """BANE background cube per Stokes. Empty unless BANE was asked for"""
    rms_cubes: dict[str, Path] = {}
    """BANE RMS cube per Stokes. Empty unless BANE was asked for"""

    @property
    def all_cubes(self) -> list[Path]:
        """Every cube written"""
        if not self.convolved:
            return []
        return [
            *self.cubes.values(),
            *self.bkg_cubes.values(),
            *self.rms_cubes.values(),
        ]


def convolve_cubes_to_common_resolution(
    cubes: dict[str, Path],
    beam_shape: BeamShape,
    output_path: Path | None = None,
    beam_cutoff: float | None = None,
    convol_suffix: str = "conv",
    fft_bane_options: FFTBANEOptions | None = None,
) -> CommonResolutionCubes:
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
        fft_bane_options (FFTBANEOptions | None, optional): When given, each convolved plane also gets a BANE background and RMS map, stacked into their own cubes. Measured after the convolution, so they describe the resolution rm-synthesis actually builds the FDF at. Defaults to None.

    Returns:
        CommonResolutionCubes: The convolved cube for each key of ``cubes``, and the BANE cubes when asked for
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

    # Measured on the convolved planes, which is the resolution the FDF is
    # built at. Resolved before the cubes are assembled, since `fitscube_options`
    # removes each plane once cubed.
    bane_maps: list[BANEMaps] = []
    if fft_bane_options is not None:
        bane_maps = task_bane_fits_image.map(
            image=convolved_planes,
            fft_bane_options=unmapped(fft_bane_options),
        ).result()

    cube_futures: dict[str, dict[str, PrefectFuture[Path]]] = {
        mode: {} for mode in (convol_suffix, "bkg", "rms")
    }
    plane_idx = 0
    for key, planes in planes_per_cube.items():
        plane_slice = slice(plane_idx, plane_idx + len(planes))
        prefix = str(cube_parent[key] / cubes[key].stem)
        cube_futures[convol_suffix][key] = task_combine_images_to_cube.submit(
            images=convolved_planes[plane_slice],
            prefix=prefix,
            mode=convol_suffix,
            fitscube_options=fitscube_options,
        )
        for mode, attribute in (("bkg", "bkg_image"), ("rms", "rms_image")):
            if bane_maps:
                cube_futures[mode][key] = task_combine_images_to_cube.submit(
                    images=[
                        getattr(maps, attribute) for maps in bane_maps[plane_slice]
                    ],
                    prefix=prefix,
                    mode=f"{convol_suffix}.{mode}",
                    fitscube_options=fitscube_options,
                )
        plane_idx += len(planes)
    assert plane_idx == len(convolved_planes), (
        f"Have {len(convolved_planes)} convolved planes across {plane_idx} channels"
    )

    resolved = {
        mode: {key: future.result() for key, future in futures.items()}
        for mode, futures in cube_futures.items()
    }
    convolved_cubes = CommonResolutionCubes(
        cubes=resolved[convol_suffix],
        convolved=True,
        bkg_cubes=resolved["bkg"],
        rms_cubes=resolved["rms"],
    )

    # The convolved planes are removed as each cube is assembled, leaving the
    # planes they were made from behind
    task_remove_files_folders.submit(*plane_paths.values()).result()

    return convolved_cubes


@task
def task_rmsynth(
    stokes_cubes: CubesForRMSynth,
    rmsynth_options: RMSynthOptions,
    error_cubes: ErrorCubesForRMSynth | None = None,
) -> RMSynth3DResults:
    from prefect_dask import get_dask_client

    # Set as the default scheduler, not just borrowed: rm-lite's per-channel
    # noise estimates are eager `dask.compute` calls, so without a default
    # client they would read whole cubes on this one worker.
    with get_dask_client():
        return run_rmsynth_3d(
            stokes_cubes=stokes_cubes,
            rmsynth_options=rmsynth_options,
            error_cubes=error_cubes,
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
