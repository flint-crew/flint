"""Prefect task wrappers for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from pathlib import Path

from flint.logging import logger
from flint.options import RMCleanOptions, RMSynthOptions
from flint.prefect.caching import task
from flint.rmsynth import (
    FDFLabel,
    RMClean3DResults,
    RMSynth3DResults,
    run_rmclean_3d,
    run_rmsynth_3d,
    write_rm_products,
)


@task
def task_rmsynth(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    stokes_i_cube: Path | None = None,
    stokes_q_weight_cube: Path | None = None,
    stokes_u_weight_cube: Path | None = None,
    stokes_i_weight_cube: Path | None = None,
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
            output_prefix=output_prefix,
            dask_client=client,
        )
