"""A continuum imaging and self-calibration workflow for
RACS-All. The intention is to accept three SBIDS from the
Low3, Mid2 and High where, by construction, the beams across
each survey have the same on-sky position. This would allow
for wide-band multi-frequency synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capn_crunch import (
    add_options_to_parser,
    create_options_from_parser,
)
from configargparse import ArgumentParser
from prefect import flow, tags, unmapped
from prefect.context import get_run_context
from prefect.futures import PrefectFuture

from flint.catalogue import verify_reference_catalogues
from flint.configuration import (
    Strategy,
    get_options_from_strategy,
    get_selfcal_round_fitscube_options,
    load_and_copy_strategy,
)
from flint.imager.channel_division import (
    ChannelDivision,
    apply_cube_division,
    channel_division_for_beams,
)
from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.ms import MS, MSsByBeam, MSSummary, find_mss
from flint.naming import (
    CASDANameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    FitsCubeOptions,
    PolFieldOptions,
    RACSAllOptions,
    dump_field_options_to_yaml,
    pol_field_options_cli_class,
    racs_all_options_to_pol_field_options,
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
    task_concatenate_holography,
    task_create_beam_summary,
    task_create_field_summary,
    task_update_field_summary,
    task_update_with_options,
)
from flint.prefect.flows.polarisation_pipeline import process_science_fields_pol
from flint.summary import BeamSummary


@dataclass
class LoopFutures:
    """Simple collector to help avoid miss-matching Future objects. Single set of MSs
    and their wsclean results."""

    mss: list[MS]
    """Futures of the MSs"""
    wsclean_result: WSCleanResult
    """Imaging results from wsclean imaging"""
    ms_summaries: list[MSSummary] | None = None
    """Results from a MS description"""


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
    if racs_all_options.run_polarisation:
        if not racs_all_options.yandasoft_container:
            raise ValueError(
                "Unable to run polarisation imaging without a yandasoft container"
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


def all_holography_available(
    racs_all_options: RACSAllOptions,
    output_science_path: Path,
) -> Path | None:

    if any(
        not isinstance(holo, Path)
        for holo in (
            racs_all_options.low_holofile,
            racs_all_options.mid_holofile,
            racs_all_options.high_holofile,
        )
    ):
        logger.info("Insufficient holography available - not concatenating together")
        return None

    if not all(
        holo is not None and holo.exists()
        for holo in (
            racs_all_options.low_holofile,
            racs_all_options.mid_holofile,
            racs_all_options.high_holofile,
        )
    ):
        msg = "Holography patch for low-, mid- and high-band data have to exist. Some are missing."
        raise ValueError(msg)

    assert isinstance(racs_all_options.low_holofile, Path), (
        "Expected Path for low-band holography"
    )

    holo_output_path = output_science_path / racs_all_options.low_holofile.name
    holo_output_path = holo_output_path.with_suffix(".concatenated.fits")
    logger.info(
        f"Holography cubes exist, output concatenated cube will be {holo_output_path=}"
    )

    return holo_output_path


@flow
def process_racs_all_field(
    racs_all_options: RACSAllOptions,
    pol_field_options: PolFieldOptions | None = None,
) -> list[PrefectFuture[Any]]:
    # returned futures are resolved by prefect to fail the flow on task failure
    terminal_futures: list[PrefectFuture[Any]] = []
    # Get the current run context to examine, provide to sub-flows
    run_context = get_run_context()

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

    # Solved once for all beams, and before any imaging, so that a target that
    # cannot make a compact cube fails the flow now instead of hours from now
    cube_division: ChannelDivision | None = None
    if racs_all_options.cube_channel_width:
        cube_division = channel_division_for_beams(
            mss_by_beam=science_mss_by_beam,
            target_width=racs_all_options.cube_channel_width,
        )

    # Polarisation may use a different channelisation to the self-cal cube, so it
    # is solved independently from the same beam frequency lists
    pol_cube_division: ChannelDivision | None = None
    if racs_all_options.run_polarisation and racs_all_options.pol_cube_channel_width:
        pol_cube_division = channel_division_for_beams(
            mss_by_beam=science_mss_by_beam,
            target_width=racs_all_options.pol_cube_channel_width,
        )

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

    round0_fitscube_options = get_selfcal_round_fitscube_options(
        strategy=strategy,
        operation="selfcal",
        current_round=0,
        final_round=racs_all_options.rounds == 0,
    )

    # Ya sea dog, we will only be handling CASDA measurementsets for the moment.
    # We will consider bandpass applications later
    _ensure_all_casda_format(mss_by_beams=science_mss_by_beam)

    holography_path = all_holography_available(
        racs_all_options=racs_all_options, output_science_path=output_science_path
    )
    if isinstance(holography_path, Path):
        holography_path = task_concatenate_holography.submit(
            output_path=holography_path,
            holo_cubes=[
                racs_all_options.low_holofile,
                racs_all_options.mid_holofile,
                racs_all_options.high_holofile,
            ],
        )
        terminal_futures.append(holography_path)

    ms_summaries: list = []
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
            ms_summaries_for_beam = preprocess_science_mss
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

            update_wsclean_options = get_options_from_strategy(
                strategy=strategy,
                mode="wsclean",
                round_info=0,
                operation="selfcal",
            )
            if cube_division is not None:
                update_wsclean_options = apply_cube_division(
                    update_wsclean_options=update_wsclean_options,
                    cube_division=cube_division,
                )

            wsclean_result = task_wsclean_imager.submit(
                in_ms=preprocess_science_mss,
                wsclean_container=racs_all_options.wsclean_container,
                update_wsclean_options=update_wsclean_options,
                update_fitscube_options=round0_fitscube_options,
            )
            imaging_results[0].append(
                LoopFutures(
                    mss=preprocess_science_mss,
                    wsclean_result=wsclean_result,
                    ms_summaries=ms_summaries_for_beam,
                )
            )
            terminal_futures.append(wsclean_result)

    beam_summaries: list[BeamSummary] = []
    for loop_result in imaging_results[0]:
        beam_summaries.extend(
            task_create_beam_summary.map(
                ms_summary=loop_result.ms_summaries,
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
                    update_masking_options=update_masking_options,
                )
                update_wsclean_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="wsclean",
                    round_info=current_round,
                    operation="selfcal",
                )
                if cube_division is not None:
                    update_wsclean_options = apply_cube_division(
                        update_wsclean_options=update_wsclean_options,
                        cube_division=cube_division,
                    )
                round_fitscube_options = get_selfcal_round_fitscube_options(
                    strategy=strategy,
                    operation="selfcal",
                    current_round=current_round,
                    final_round=final_round,
                )
                wsclean_result = task_wsclean_imager.submit(
                    in_ms=cal_mss,
                    wsclean_container=racs_all_options.wsclean_container,
                    fits_mask=fits_beam_mask,
                    update_wsclean_options=update_wsclean_options,
                    update_fitscube_options=round_fitscube_options,
                )
                imaging_results[current_round].append(
                    LoopFutures(mss=cal_mss, wsclean_result=wsclean_result)
                )
                terminal_futures.append(wsclean_result)

    if racs_all_options.yandasoft_container:
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
                additional_linmos_suffix_str=additional_linmos_suffix,
                holofile=holography_path,  # indicate in output linmos name no selfcal
            )
            logger.info(
                f"Self-cal round {selfcal_round}, number of parsets {len(parsets)}"
            )
            terminal_futures.extend(parsets)

            if racs_all_options.aegean_container:
                logger.info(f"Running aegean on round {selfcal_round}")
                aegean_outputs = task_run_bane_and_aegean.submit(
                    image=parsets[-1],
                    aegean_container=racs_all_options.aegean_container,
                )  # type: ignore
                field_summary = task_update_field_summary.submit(
                    field_summary=field_summary,
                    aegean_outputs=aegean_outputs,
                    round=selfcal_round if selfcal_round > 0 else None,
                )
                terminal_futures.append(field_summary)

                if selfcal_round in (0, racs_all_options.rounds):
                    val_results = validation_items(
                        field_summary=field_summary,
                        aegean_outputs=aegean_outputs,
                        reference_catalogue_directory=racs_all_options.reference_catalogue_directory,
                    )
                    if val_results:
                        terminal_futures.extend(val_results)

        # There are some blocking calls in the cube coadding, so do it
        # as late as possible, ya sea rat
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

                final_round_fitscube_options = get_options_from_strategy(
                    strategy=strategy,
                    operation="selfcal",
                    mode="fitscube",
                    round_info=cube_add_round,
                )
                fits_cube_options = FitsCubeOptions(compress=True).with_options(
                    **final_round_fitscube_options
                )

                linmos_cubes = create_convolve_linmos_cubes(
                    wsclean_results=cube_results,  # type: ignore
                    field_options=racs_all_options,
                    fitscube_options=fits_cube_options,
                    current_round=(
                        racs_all_options.rounds if racs_all_options.rounds else None
                    ),
                    additional_linmos_suffix_str="cube",
                    holofile=holography_path,
                )
                terminal_futures.extend(linmos_cubes)

    if racs_all_options.run_polarisation:
        with tags("polarisation"):
            resolved_pol_field_options = (
                pol_field_options
                if pol_field_options is not None
                else racs_all_options_to_pol_field_options(racs_all_options)
            )
            resolved_pol_field_options = resolved_pol_field_options.with_options(
                holofile=holography_path.result()
                if isinstance(holography_path, PrefectFuture)
                else holography_path
            )
            # Hand down the final round's per-beam self-calibrated MSs directly, rather
            # than having the polarisation flow rediscover MSs by globbing the output
            # directory. These are still Prefect futures - passing them here is what
            # makes prefect wait for the self-cal loop to finish before imaging starts.
            final_round_mss_by_beam: MSsByBeam = tuple(
                tuple([ms.result() for ms in beam_result.mss])
                for beam_result in imaging_results[racs_all_options.rounds]
            )
            low_sbid = get_sbid_from_path(path=racs_all_options.low_data)

            # sub-flows do no inherit the task runner, they use the specified
            # running in their decorator flow argument. Overwrite it here with
            # the current runner
            from prefect_dask import DaskTaskRunner

            sub_flow_runner = DaskTaskRunner(
                cluster_class=run_context.task_runner.cluster_class,
                cluster_kwargs=run_context.task_runner.cluster_kwargs,
            )

            pol_futures = process_science_fields_pol.with_options(
                task_runner=sub_flow_runner,
                name=f"RACS All polarisation -- {low_sbid}",
            )(
                flint_ms_directory=output_science_path,
                pol_field_options=resolved_pol_field_options,
                cube_division=pol_cube_division,
                mss_by_beam=final_round_mss_by_beam,
                wait_for=terminal_futures,
            )

            # pol_futures = process_science_fields_pol.fn(
            #     flint_ms_directory=output_science_path,
            #     pol_field_options=resolved_pol_field_options,
            #     cube_division=pol_cube_division,
            #     mss_by_beam=final_round_mss_by_beam,
            # )

            terminal_futures.extend(pol_futures)

    return terminal_futures


def setup_run_racs_all_field(
    cluster_config: Path,
    racs_all_options: RACSAllOptions,
    pol_field_options: PolFieldOptions | None = None,
) -> None:
    """The main launch script for the RACS-All processing flow

    Args:
        cluster_config (Path): Path to the dask configuration yaml file to define the cluster
        racs_all_options (RACSAllOptions): Options around the processing of RACS-All field
        pol_field_options (PolFieldOptions | None, optional): Options for the polarisation imaging pipeline, used if ``racs_all_options.run_polarisation`` is set. Derived from ``racs_all_options`` if not provided. Defaults to None.
    """

    low_sbid = get_sbid_from_path(path=racs_all_options.low_data)

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_racs_all_field.with_options(
        name=f"RACS All -- {low_sbid}", task_runner=dask_task_runner
    )(racs_all_options=racs_all_options, pol_field_options=pol_field_options)


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
    parser = add_options_to_parser(
        parser=parser,
        options_class=pol_field_options_cli_class(RACSAllOptions),
        description="Polarisation processing options",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    racs_all_options = create_options_from_parser(
        parser_namespace=args, options_class=RACSAllOptions
    )
    pol_field_options = create_options_from_parser(
        parser_namespace=args, options_class=PolFieldOptions
    )

    setup_run_racs_all_field(
        cluster_config=args.cluster_config,
        racs_all_options=racs_all_options,
        pol_field_options=pol_field_options,
    )


if __name__ == "__main__":
    cli()
