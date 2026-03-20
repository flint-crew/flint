"""A continuum imaging and self-calibration workflow for
RACS-All. The intention is to accept three SBIDS from the
Low3, Mid2 and High where, by construction, the beams across
each survey have the same on-sky position. This would allow
for wide-band multi-frequency synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from configargparse import ArgumentParser
from prefect import flow, tags, unmapped

from flint.catalogue import verify_reference_catalogues
from flint.configuration import (
    Strategy,
    get_options_from_strategy,
    load_and_copy_strategy,
)
from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.ms import MS, find_mss
from flint.naming import (
    CASDANameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    RACSAllOptions,
    add_options_to_parser,
    create_options_from_parser,
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    create_convol_linmos_images,
    create_convolve_linmos_cubes,
    task_copy_and_preprocess_casda_askap_ms,
    task_create_image_mask_model,
    task_flag_ms_aoflagger,
    task_gaincal_applycal_ms,
    task_potato_peel,
    task_run_bane_and_aegean,
    task_wsclean_imager,
    validation_items,
)
from flint.prefect.common.ms import task_describe_ms
from flint.prefect.common.utils import (
    task_create_beam_summary,
    task_create_field_summary,
    task_update_field_summary,
    task_update_with_options,
)

MSsByBeam: TypeAlias = tuple[tuple[MS, ...], ...]


@dataclass
class LoopFutures:
    """Simple collector to help avoid miss-matching Future objects. Single set of MSs
    and their wsclean results."""

    mss: list[MS]
    """Futures of the MSs"""
    wsclean_result: WSCleanResult
    """Imaging results from wsclean imaging"""


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

    # For the moment we make sure that this is provided. Can consider moving to mandatory argument in
    # the model definition
    assert (
        isinstance(racs_all_options.wsclean_container, Path)
        and racs_all_options.wsclean_container.exists()
    ), "Missing wsclean container path"


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
    """Matchh the input MSs across the three input bands together. This is done based on the
    information in the file name, assuming the input names are recorgnised.

    Args:
        low_mss (tuple[MS, ...]): Measurement sets in the low band
        mid_mss (tuple[MS, ...]): Measurement sets in the mid band
        high_mss (tuple[MS, ...]): Measurement sets in the high band

    Raises:
        ValueError: _description_

    Returns:
        tuple[tuple[MS, ...], ...]: Measurement sets grouped by beam. They are sorted in ascending order.
    """

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


def _ensure_all_casda_format(mss_by_beams: MSsByBeam) -> None:
    """A simple check to ensure all are CASDA measurement sets

    Args:
        mss_by_beams (MSsByBeam): The by beams MSs

    Raises:
        ValueError: Raised if an input MS is not a CASDA format
    """

    for mss_in_beam in mss_by_beams:
        for ms in mss_in_beam:
            logger.info(ms)
            components = extract_components_from_name(name=ms.path)
            if not isinstance(components, CASDANameComponents):
                raise ValueError(f"Was expecting only CASDA MSs, got {components}")


@flow
def process_racs_all_field(racs_all_options: RACSAllOptions) -> None:
    # Any sanity checks will go in here, mateee
    _check_racs_all_options(racs_all_options=racs_all_options)
    output_science_path = _check_create_output_science_path(
        science_path=racs_all_options.low_data, output_path=None, check_exists=True
    )
    logger.info(f"Processing directory is {output_science_path=}")

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

    science_mss_by_beam: MSsByBeam = match_beams_across_bands(
        low_mss=low_band_mss, mid_mss=mid_band_mss, high_mss=high_band_mss
    )
    logger.info(f"Will be processing {len(science_mss_by_beam)} beams")

    dump_field_options_to_yaml(
        output_path=add_timestamp_to_path(
            input_path=output_science_path / "racs_all_options.yaml"
        ),
        field_options=racs_all_options,
    )

    strategy: Strategy | None = load_and_copy_strategy(
        output_split_science_path=output_science_path,
        imaging_strategy=racs_all_options.imaging_strategy,
    )
    logger.info(f"Remove this later {strategy=}")

    # Ya sea dog, we will only be handling CASDA measurementsets for the moment.
    # We will consider bandpass applications later
    _ensure_all_casda_format(mss_by_beams=science_mss_by_beam)

    ms_summaries = []
    imaging_results: dict[int, list[LoopFutures]] = {}
    imaging_results[0] = []
    with tags("no-selfcal"):
        for science_mss in science_mss_by_beam:
            preprocess_science_mss = task_copy_and_preprocess_casda_askap_ms.map(
                casda_ms=science_mss,
                casa_container=racs_all_options.casa_container,
                output_directory=output_science_path,
            )
            preprocess_science_mss = task_describe_ms.map(
                ms=preprocess_science_mss, attach_ms=True
            )
            ms_summaries.extend(preprocess_science_mss)
            if racs_all_options.flagger_container is not None:
                preprocess_science_mss = task_flag_ms_aoflagger.map(
                    ms=preprocess_science_mss,
                    container=racs_all_options.flagger_container,
                )

            if racs_all_options.potato_container:
                # The call into potato peel task has two potential update option keywords.
                # So for the moment we will not use the task decorated version.
                potato_wsclean_init = get_options_from_strategy(
                    strategy=strategy, mode="wsclean", round_info=0, operation="selfcal"
                )
                potato_peel_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="potatopeel",
                    round_info=0,
                    operation="selfcal",
                )
                preprocess_science_mss = task_potato_peel.map(
                    ms=preprocess_science_mss,
                    potato_container=racs_all_options.potato_container,
                    update_wsclean_options=unmapped(potato_wsclean_init),
                    update_potato_peel_options=unmapped(potato_peel_options),
                )

            wsclean_result = task_wsclean_imager.submit(
                in_ms=preprocess_science_mss,
                wsclean_container=racs_all_options.wsclean_container,
                update_wsclean_options=unmapped(
                    get_options_from_strategy(
                        strategy=strategy,
                        mode="wsclean",
                        round_info=0,
                        operation="selfcal",
                    )
                ),
            )
            imaging_results[0].append(
                LoopFutures(
                    mss=preprocess_science_mss,
                    wsclean_result=wsclean_result,
                )
            )

    beam_summaries = []
    for loop_result in imaging_results[0]:
        beam_summaries.extend(
            task_create_beam_summary.map(
                ms=loop_result.mss,
                image_set=unmapped(loop_result.wsclean_result),
            )
        )

    # Using ms summary objects as basis of field summary as MSs can change names
    # or be deleted throughout processing. TThis allows for no `wait_for` usage.
    field_summary = task_create_field_summary.submit(
        mss=None,  # Constructed from the ms summaries
        cal_sbid_path=None,  # CASDA MSs have solutions applied
        holography_path=None,  # No unified holography (yet, mate)
        ms_summaries=ms_summaries,
    )

    field_summary = task_update_with_options.submit(
        input_object=field_summary, beam_summaries=beam_summaries
    )

    for current_round in range(1, racs_all_options.rounds + 1):
        final_round = current_round == racs_all_options.rounds
        if final_round:
            logger.info("This is the final round of selcalibtation")

        with tags(f"selfcal-{current_round}"):
            round_imaging_results = imaging_results[current_round - 1]
            imaging_results[current_round] = []
            for beam_imaging_results in round_imaging_results:
                update_gain_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="gaincal",
                    round_info=current_round,
                    operation="selfcal",
                )
                cal_mss = task_gaincal_applycal_ms.map(
                    ms=beam_imaging_results.mss,
                    selfcal_round=current_round,
                    archive_input_ms=racs_all_options.zip_ms,
                    skip_selfcal=False,
                    rename_ms=racs_all_options.rename_ms,
                    archive_cal_table=True,
                    casa_container=racs_all_options.casa_container,
                    update_gain_cal_options=unmapped(update_gain_options),
                    wait_for=beam_imaging_results.wsclean_result,
                )
                update_masking_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="masking",
                    round_info=current_round,
                    operation="selfcal",
                )

                # TODO: An explicit trminder to add per-beam aegean source finding, or
                # just make the mac for this explicit and required
                assert update_masking_options["flood_fill_use_mac"], (
                    "Currently only support mac based masking (no per-beam aegean)"
                )

                fits_beam_mask = task_create_image_mask_model.submit(
                    image=beam_imaging_results.wsclean_result,
                    image_products=None,  # Mac works on apparent brightness
                    update_masking_options=unmapped(update_masking_options),
                )
                wsclean_result = task_wsclean_imager.submit(
                    in_ms=cal_mss,
                    wsclean_container=racs_all_options.wsclean_container,
                    fits_mask=fits_beam_mask,
                    update_wsclean_options=unmapped(
                        get_options_from_strategy(
                            strategy=strategy,
                            mode="wsclean",
                            round_info=current_round,
                            operation="selfcal",
                        )
                    ),
                )
                imaging_results[current_round].append(
                    LoopFutures(mss=cal_mss, wsclean_result=wsclean_result)
                )

    if racs_all_options.yandasoft_container:
        if racs_all_options.coadd_cubes:
            with tags("cubes"):
                cube_add_round = racs_all_options.rounds
                cube_add_round = racs_all_options.rounds

                assert cube_add_round is not None, (
                    f"{racs_all_options.rounds=}, but needs to be positive"
                )

                cube_results = [
                    beam_result.wsclean_result
                    for beam_result in imaging_results[cube_add_round]
                ]

                create_convolve_linmos_cubes(
                    wsclean_results=cube_results,  # type: ignore
                    field_options=racs_all_options,
                    current_round=(
                        racs_all_options.rounds if racs_all_options.rounds else None
                    ),
                    additional_linmos_suffix_str="cube",
                )

        for selfcal_round, final_beam_imaging_results in imaging_results.items():
            additional_linmos_suffix = (
                "noselfcal" if selfcal_round == 0 else f"round{selfcal_round}"
            )
            wsclean_results = [
                final_beam_imaging_result.wsclean_result
                for final_beam_imaging_result in final_beam_imaging_results
            ]
            parsets = create_convol_linmos_images(
                wsclean_results=wsclean_results,
                field_options=racs_all_options,
                field_summary=field_summary,
                additional_linmos_suffix_str=additional_linmos_suffix,  # indicate in output linmos name no selfcal
            )
            logger.info(
                f"Self-cal round {selfcal_round}, number of parsets {len(parsets)}"
            )

            if racs_all_options.aegean_container:
                logger.info(f"Running aegean on round {selfcal_round}")
                aegean_outputs = task_run_bane_and_aegean.submit(
                    image=parsets[-1],
                    aegean_container=unmapped(racs_all_options.aegean_container),
                )  # type: ignore
                field_summary = task_update_field_summary.submit(
                    field_summary=field_summary,
                    aegean_outputs=aegean_outputs,
                    round=selfcal_round if selfcal_round > 0 else None,
                )
                validation_items(
                    field_summary=field_summary,
                    aegean_outputs=aegean_outputs,
                    reference_catalogue_directory=racs_all_options.reference_catalogue_directory,
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
