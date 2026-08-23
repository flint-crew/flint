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
from flint.naming import create_name_from_common_fields, get_sbid_from_path
from flint.options import RMCleanOptions, RMSynthFieldOptions, RMSynthOptions
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.rmsynth import (
    task_rmclean,
    task_rmsynth,
    task_write_rm_products,
)
from flint.prefect.common.utils import task_archive_sbid
from flint.rmsynth import needs_rmclean


@flow(name="Flint RM-Synthesis Pipeline")
def process_rmsynth(rmsynth_field_options: RMSynthFieldOptions) -> list[Path]:
    if (
        rmsynth_field_options.stokes_q_cube is None
        or rmsynth_field_options.stokes_u_cube is None
    ):
        raise ValueError(
            "stokes_q_cube and stokes_u_cube are required. The racs-all flow sets "
            "them from the polarisation stage."
        )

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
        stokes_i_error_cube=rmsynth_field_options.stokes_i_error_cube,
    )

    run_clean = needs_rmclean(
        cube_products=rmsynth_field_options.cube_products,
        moment_products=rmsynth_field_options.moment_products,
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
    if rmsynth_field_options.output_path is not None:
        rmsynth_field_options.output_path.mkdir(parents=True, exist_ok=True)
        output_prefix = rmsynth_field_options.output_path / output_prefix.name

    output_paths = task_write_rm_products.submit(
        synth_results=synth_result,
        clean_results=clean_result,
        stokes_q_cube=rmsynth_field_options.stokes_q_cube,
        rmsynth_options=rmsynth_options,
        cube_products=rmsynth_field_options.cube_products,
        moment_products=rmsynth_field_options.moment_products,
        output_prefix=output_prefix,
    )

    written_paths = output_paths.result()

    if rmsynth_field_options.sbid_copy_path:
        task_archive_sbid.submit(
            science_folder_path=output_prefix.parent,
            copy_path=rmsynth_field_options.sbid_copy_path,
        ).result()

    return written_paths


def setup_run_rmsynth(
    cluster_config: str | Path, rmsynth_field_options: RMSynthFieldOptions
) -> None:
    if (
        rmsynth_field_options.sbid_copy_path
        and rmsynth_field_options.stokes_q_cube is not None
    ):
        science_sbid = get_sbid_from_path(path=rmsynth_field_options.stokes_q_cube)
        rmsynth_field_options = rmsynth_field_options.with_options(
            sbid_copy_path=rmsynth_field_options.sbid_copy_path / f"{science_sbid}"
        )

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
