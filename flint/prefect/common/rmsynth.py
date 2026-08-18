"""Prefect task wrappers for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from pathlib import Path

from prefect import task

from flint.logging import logger
from flint.options import RMCleanOptions, RMSynthOptions
from flint.rmsynth import (
    FDFLabel,
    RMClean3DResults,
    RMSynth3DResults,
    run_rmclean_3d,
    run_rmsynth_3d,
    write_rm_products,
)

task_rmsynth = task(run_rmsynth_3d)
task_rmclean = task(run_rmclean_3d)


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
