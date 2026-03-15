"""A continuum imaging and self-calibration workflow for
RACS-All. The intention is to accept three SBIDS from the
Low3, Mid2 and High where, by construction, the beams across
each survey have the same on-sky position. This would allow
for wide-band multi-frequency synthesis.
"""

from __future__ import annotations

from pathlib import Path

from configargparse import ArgumentParser
from prefect import flow

from flint.catalogue import verify_reference_catalogues
from flint.logging import logger
from flint.ms import MS, find_mss
from flint.naming import (
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    RACSAllOptions,
    add_options_to_parser,
    create_options_from_parser,
)
from flint.prefect.clusters import get_dask_runner


def _check_racs_all_options(racs_all_options: RACSAllOptions) -> None:
    """Some basic checks around the inputs to the racs all options. ValueError is raised
    if any check fails

    Args:
        racs_all_options (RACSAllOptions): The options to consider

    Raises:
        ValueError: Raised if any check fails, which specific output to error presented
    """

    if (
        racs_all_options.imaging_strategy is not None
        and not racs_all_options.imaging_strategy.exists()
    ):
        raise ValueError(
            f"Imaging strategy file {racs_all_options.imaging_strategy} is set, but the path does not exist"
        )
    if racs_all_options.reference_catalogue_directory:
        if not verify_reference_catalogues(
            reference_directory=racs_all_options.reference_catalogue_directory
        ):
            raise ValueError(
                f"{racs_all_options.reference_catalogue_directory=} does not appear to be valid. Check for reference catalogues"
            )
    if racs_all_options.rounds is not None:
        if racs_all_options.rounds >= 1 and racs_all_options.casa_container is None:
            raise ValueError(
                "CASA Container needs to be set if self-calibration is to be performed"
            )
    if racs_all_options.coadd_cubes:
        if (
            racs_all_options.yandasoft_container is None
            or not racs_all_options.yandasoft_container
        ):
            raise ValueError(
                "Unable to create linmos cubes without a yandasoft container"
            )


def _check_create_output_science_path(
    science_path: Path, output_path: Path | None = None, check_exists: bool = True
) -> Path:
    """Create the output path that the science MS and subsequent processing
     will take place from.

    Args:
        science_path (Path): The directory that contains the MSs for science processing of one of the bands.
        output_path (Path): Where the output MSs will be written to and processed
        check_exists (bool, optional): Should we check to make sure output directory does not exist. Defaults to True.

    Raises:
        ValueError: Raised when the output directory exists

    Returns:
        Path: The output directory
    """

    science_folder_name = science_path.name
    assert str(science_folder_name).isdigit(), (
        f"We require the parent directory to be the SBID (all digits), got {science_folder_name=}"
    )
    if output_path is None:
        logger.info(
            "No explicit output processing directory specified. Processing in current working directory."
        )
        output_path = Path.cwd()

    output_split_science_path = (
        Path(output_path / science_folder_name).absolute().resolve()
    )

    if check_exists and output_split_science_path.exists():
        logger.critical(
            f"{output_split_science_path=} already exists. It should not. Exiting. "
        )
        raise ValueError("Output science directory already exists. ")

    logger.info(f"Creating {output_split_science_path!s}")
    output_split_science_path.mkdir(parents=True)

    return output_split_science_path


def match_beams_across_bands(
    low_mss: tuple[MS, ...], mid_mss: tuple[MS, ...], high_mss: tuple[MS, ...]
) -> tuple[tuple[MS, ...], ...]:

    from collections import defaultdict

    matched_mss = defaultdict(list)

    for band_mss in (low_mss, mid_mss, high_mss):
        for ms in band_mss:
            components = extract_components_from_name(name=ms.path)
            if components.beam is None:
                raise ValueError(f"Beam is not found for {ms.path}")
            matched_mss[int(components.beam)].append(ms)

    sorted_beam_idxs = sorted(matched_mss.keys())
    logger.info("Matched MSs to beams across bands")
    for beam_idx in sorted_beam_idxs:
        for ms in matched_mss[beam_idx]:
            logger.info(f"{beam_idx:02d}: {ms.path}")
        else:
            logger.info("")

    return tuple([tuple(beam_mss) for _, beam_mss in matched_mss.items()])


@flow
def process_racs_all_field(racs_all_options: RACSAllOptions) -> None:
    # Any sanity checks will go in here, mateee
    _check_racs_all_options(racs_all_options=racs_all_options)
    output_scienc_path = _check_create_output_science_path(
        science_path=racs_all_options.low_data, output_path=None, check_exists=True
    )
    logger.info(f"Processing directory is {output_scienc_path=}")

    low_band_mss = find_mss(
        mss_parent_path=racs_all_options.low_data,
        expected_ms_count=racs_all_options.expected_ms,
    )
    mid_band_mss = find_mss(
        mss_parent_path=racs_all_options.mid_data,
        expected_ms_count=racs_all_options.expected_ms,
    )
    high_band_mss = find_mss(
        mss_parent_path=racs_all_options.high_data,
        expected_ms_count=racs_all_options.expected_ms,
    )

    match_beams_across_bands(
        low_mss=low_band_mss, mid_mss=mid_band_mss, high_mss=high_band_mss
    )


def setup_run_racs_all_field(
    cluster_config: Path, racs_all_options: RACSAllOptions
) -> None:
    """The main launch script for the RACS-All processing flow

    Args:
        cluster_config (Path): Path to the dask configuration yaml file to define the cluster
        racs_all_options (RACSAllOptions): Options around the processing of RACS-All field
    """

    low_sbid = get_sbid_from_path(path=racs_all_options.low_data)

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_racs_all_field.with_options(
        name=f"RACS All -- {low_sbid}", task_runner=dask_task_runner
    )(racs_all_options=racs_all_options)


def get_parser() -> ArgumentParser:
    """Constructs the CLI argument parser for this flow

    Returns:
        ArgumentParser: The argument parser
    """

    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )

    parser.add_argument(
        "--cluster-config",
        type=Path,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(parser=parser, options_class=RACSAllOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    racs_all_options = create_options_from_parser(
        parser_namespace=args, options_class=RACSAllOptions
    )

    setup_run_racs_all_field(
        cluster_config=args.cluster_config, racs_all_options=racs_all_options
    )


if __name__ == "__main__":
    cli()
