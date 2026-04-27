"""A toy around idea"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from prefect import flow

from flint.configuration import get_options_from_strategy, load_strategy_yaml
from flint.logging import logger
from flint.ms import MS
from flint.naming import get_sbid_from_path
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    task_create_image_mask_model,
    task_run_bane_and_aegean,
    task_wsclean_imager,
)


class BeamCubletOptions(BaseOptions):
    """Options around the beam cublet toy image"""

    ms_path: Path
    """The measurement set to be images"""
    strategy: Path
    """The strategy file with options to use"""
    wsclean_container: Path
    """Path to the wsclean container"""
    aegean_container: Path
    """Path to aegean container"""
    cluster_config: Path
    """Path to the Dask configuration file"""
    column: str = "CORRECTED_DATA"
    """The name of the column to begin operation from"""


@flow
def flow_beam_cublet_image(beam_cublet_options: BeamCubletOptions) -> None:
    logger.info("Attempting beam cublet imaging")

    ms = MS(path=beam_cublet_options.ms_path, column=beam_cublet_options.column)
    logger.info(f"Processing {ms=}")

    strategy = load_strategy_yaml(input_yaml=beam_cublet_options.strategy)

    wsclean_options = get_options_from_strategy(
        strategy=strategy, operation="cubletimage"
    )
    beam_wsclean_result = task_wsclean_imager.submit(
        in_ms=ms,
        update_wsclean_options=wsclean_options,
        wsclean_container=beam_cublet_options.wsclean_container,
    )
    aegean_results = task_run_bane_and_aegean(
        image=beam_wsclean_result, aegean_container=beam_cublet_options.aegean_container
    )

    masking_options = get_options_from_strategy(
        strategy=strategy, operation="cubletimage"
    )
    fits_mask = task_create_image_mask_model(
        image=beam_wsclean_result,
        image_products=aegean_results,
        update_masking_options=masking_options,
    )
    logger.info(f"{fits_mask}")


def setup_beam_cublet_workflow(beam_cublet_options: BeamCubletOptions) -> None:

    dask_runner = get_dask_runner(cluster=beam_cublet_options.cluster_config)

    sbid = get_sbid_from_path(path=beam_cublet_options.ms_path)

    flow_beam_cublet_image.with_options(
        name=f"Flint Beam Cublet Image -- {sbid}", task_runner=dask_runner
    )()


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description="A toy around cublet image workflow")

    parser = add_options_to_parser(parser=parser, options_class=BeamCubletOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    beam_cublet_options = create_options_from_parser(
        parser_namespace=args, options_class=BeamCubletOptions
    )

    setup_beam_cublet_workflow(beam_cublet_options=beam_cublet_options)


if __name__ == "__main__":
    cli()
