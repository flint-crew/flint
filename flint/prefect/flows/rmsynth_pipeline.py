"""Standalone RM-synthesis (and, if requested, RM-CLEAN) pipeline: given
already-imaged Stokes Q/U (and optionally I) FITS cubes, write the requested
Faraday dispersion function (FDF) cube and moment map products. See
``flint.rmsynth``.
"""

from __future__ import annotations

from pathlib import Path

from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from prefect import flow

from flint.configuration import get_options_from_strategy, load_and_copy_strategy
from flint.logging import logger
from flint.naming import create_name_from_common_fields
from flint.options import RMCleanOptions, RMSynthFieldOptions, RMSynthOptions
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.rmsynth import (
    task_rmclean,
    task_rmsynth,
    task_write_rm_products,
)


@flow(name="Flint RM-Synthesis Pipeline")
def process_rmsynth(rmsynth_field_options: RMSynthFieldOptions) -> list[Path]:
    strategy = load_and_copy_strategy(
        output_split_science_path=rmsynth_field_options.stokes_q_cube.parent,
        imaging_strategy=rmsynth_field_options.imaging_strategy,
    )

    if (
        not rmsynth_field_options.cube_products
        and not rmsynth_field_options.moment_products
    ):
        logger.info("No RM-synthesis products requested, skipping.")
        return []

    rmsynth_options = RMSynthOptions(
        **get_options_from_strategy(
            strategy=strategy, operation="rmsynth", mode="rmsynth"
        )
    )
    rmclean_options = RMCleanOptions(
        **get_options_from_strategy(
            strategy=strategy, operation="rmsynth", mode="rmclean"
        )
    )

    synth_result = task_rmsynth.submit(
        stokes_q_cube=rmsynth_field_options.stokes_q_cube,
        stokes_u_cube=rmsynth_field_options.stokes_u_cube,
        rmsynth_options=rmsynth_options,
        stokes_i_cube=rmsynth_field_options.stokes_i_cube,
    )

    run_clean = any(
        label in ("clean", "model")
        for label in (
            *rmsynth_field_options.cube_products,
            *rmsynth_field_options.moment_products,
        )
    )
    clean_result = (
        task_rmclean.submit(
            rm_synth_results=synth_result, rmclean_options=rmclean_options
        )
        if run_clean
        else None
    )

    output_prefix = create_name_from_common_fields(
        in_paths=(
            rmsynth_field_options.stokes_q_cube,
            rmsynth_field_options.stokes_u_cube,
        )
    )

    output_paths = task_write_rm_products.submit(
        synth_results=synth_result,
        clean_results=clean_result,
        stokes_q_cube=rmsynth_field_options.stokes_q_cube,
        rmsynth_options=rmsynth_options,
        rmclean_options=rmclean_options,
        cube_products=rmsynth_field_options.cube_products,
        moment_products=rmsynth_field_options.moment_products,
        output_prefix=output_prefix,
    )

    return output_paths.result()


def setup_run_rmsynth(
    cluster_config: str | Path, rmsynth_field_options: RMSynthFieldOptions
) -> None:
    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_rmsynth.with_options(task_runner=dask_task_runner)(
        rmsynth_field_options=rmsynth_field_options
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(
        parser=parser,
        options_class=RMSynthFieldOptions,
        description="RM-synthesis processing options",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    rmsynth_field_options = create_options_from_parser(
        parser_namespace=args, options_class=RMSynthFieldOptions
    )

    setup_run_rmsynth(
        cluster_config=args.cluster_config,
        rmsynth_field_options=rmsynth_field_options,
    )


if __name__ == "__main__":
    cli()
