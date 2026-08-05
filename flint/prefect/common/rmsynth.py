"""Prefect task wrapper for RM-synthesis (``flint.rmsynth``).

Kept separate from ``flint.prefect.common.imaging`` (which every flow already
imports) so that the ``rm-lite`` optional dependency is only required by flows
that actually import this module.
"""

from __future__ import annotations

from pathlib import Path

from prefect import task

from flint.logging import logger
from flint.options import RMCleanOptions, RMSynthOptions
from flint.rmsynth import FDFLabel, rmsynth_and_write_products


@task
def task_rmsynth_and_write_products(
    stokes_q_cube: Path,
    stokes_u_cube: Path,
    rmsynth_options: RMSynthOptions,
    rmclean_options: RMCleanOptions,
    cube_products: list[FDFLabel],
    moment_products: list[FDFLabel],
    output_prefix: Path,
    stokes_i_cube: Path | None = None,
) -> list[Path]:
    """Run RM-synthesis, computing across the Dask cluster backing the flow's
    ``DaskTaskRunner`` rather than just this task's own worker -- mirrors how
    ``task_crystalball_to_ms`` hands its distributed Client to crystalball
    (``flint/prefect/common/predict.py``).
    """
    from prefect_dask import get_dask_client

    with get_dask_client(set_as_default=False) as client:
        logger.info("Obtained the Client supporting the DaskTaskRunner.")
        return rmsynth_and_write_products(
            stokes_q_cube=stokes_q_cube,
            stokes_u_cube=stokes_u_cube,
            rmsynth_options=rmsynth_options,
            rmclean_options=rmclean_options,
            cube_products=cube_products,
            moment_products=moment_products,
            output_prefix=output_prefix,
            stokes_i_cube=stokes_i_cube,
            dask_client=client,
        )
