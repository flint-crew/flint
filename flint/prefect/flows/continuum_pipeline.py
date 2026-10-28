"""A prefect based pipeline that:
- will perform bandpass calibration with PKS B1934-638 data, or from a derived sky-model
- copy and apply to science field
- optionally phase calibrate the science fields against a reference sky-model
- image and self-calibration the science fields
- run aegean source finding
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from eye_patch.naming import FITSMaskNames
from prefect import flow, tags, unmapped
from prefect.futures import PrefectFuture

from flint.calibrate.aocalibrate import find_existing_solutions
from flint.catalogue import verify_reference_catalogues
from flint.configuration import (
    FitsCubeOptions,
    Strategy,
    get_options_from_strategy,
    get_selfcal_round_fitscube_options,
    load_and_copy_strategy,
)
from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.masking import consider_beam_mask_round
from flint.ms import MS, find_mss
from flint.naming import (
    CASDANameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    FieldOptions,
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    create_convol_linmos_images,
    create_convolve_linmos_cubes,
    task_copy_and_preprocess_casda_askap_ms,
    task_create_apply_solutions_cmd,
    task_create_image_mask_model,
    task_flag_ms_aoflagger,
    task_gaincal_applycal_ms,
    task_potato_peel,
    task_preprocess_askap_ms,
    task_rename_column_in_ms,
    task_run_bane_and_aegean,
    task_select_solution_for_ms,
    task_split_by_field,
    task_wsclean_imager,
    task_zip_ms,
    validation_items,
)
from flint.prefect.common.ms import (
    task_add_model_source_list_to_ms,
    task_describe_ms,
    task_jolly_roger_tractor,
)
from flint.prefect.common.predict import calibrate_mss_against_sky_model
from flint.prefect.common.utils import (
    task_archive_sbid,
    task_create_beam_summary,
    task_create_field_summary,
    task_flag_antenna_from_casda_bandpass_table,
    task_flatten,
    task_update_field_summary,
    task_update_with_options,
)
from flint.selfcal.utils import consider_skip_selfcal_on_round


def _check_field_options(field_options: FieldOptions) -> None:
    run_aegean = (
        False if field_options.aegean_container is None else field_options.run_aegean
    )
    if (
        field_options.imaging_strategy is not None
        and not field_options.imaging_strategy.exists()
    ):
        raise ValueError(
            f"Imaging strategy file {field_options.imaging_strategy} is set, but the path does not exist"
        )
    if field_options.use_beam_masks is True and run_aegean is False:
        raise ValueError(
            "run_aegean and aegean container both need to be set is beam masks is being used. "
        )
    if field_options.reference_catalogue_directory:
        if not verify_reference_catalogues(
            reference_directory=field_options.reference_catalogue_directory
        ):
            raise ValueError(
                f"{field_options.reference_catalogue_directory=} does not appear to be valid. Check for reference catalogues"
            )
    if field_options.rounds is not None:
        if field_options.rounds >= 1 and field_options.casa_container is None:
            raise ValueError(
                "CASA Container needs to be set if self-calibration is to be performed"
            )
    if field_options.coadd_cubes:
        if (
            field_options.yandasoft_container is None
            or not field_options.yandasoft_container
        ):
            raise ValueError(
                "Unable to create linmos cubes without a yandasoft container"
            )
    if field_options.run_skymodel_calibration:
        # The catalogues themselves are verified by the reference_catalogue_directory check above
        if field_options.reference_catalogue_directory is None:
            raise ValueError(
                "reference_catalogue_directory needs to be set if run_skymodel_calibration is True"
            )
        if field_options.calibrate_container is None:
            raise ValueError(
                "calibrate_container needs to be set if run_skymodel_calibration is True"
            )
        if field_options.casa_container is None:
            raise ValueError(
                "casa_container needs to be set if run_skymodel_calibration is True"
            )


def _check_create_output_split_science_path(
    science_path: Path, split_path: Path, check_exists: bool = True
) -> Path:
    """Create the output path that the science MSs will be placed.

    Args:
        science_path (Path): The directory that contains the MSs for science processing
        split_path (Path): Where the output MSs will be written to and processed
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
    output_split_science_path = (
        Path(split_path / science_folder_name).absolute().resolve()
    )

    if check_exists and output_split_science_path.exists():
        logger.critical(
            f"{output_split_science_path=} already exists. It should not. Exiting. "
        )
        raise ValueError("Output science directory already exists. ")

    logger.info(f"Creating {output_split_science_path!s}")
    output_split_science_path.mkdir(parents=True)

    return output_split_science_path


def preprocess_science_mss(
    science_mss: Sequence[MS],
    field_options: FieldOptions,
    output_path: Path,
    bandpass_path: Path | None = None,
) -> list[PrefectFuture[MS]]:
    """Copy the raw science MSs into the output directory, calibrated and flagged ready for imaging."""
    # TODO: This feels a little too much like that feeling of being out
    # at sea for to long. Should refactor (or mask a EMU only).
    if isinstance(
        extract_components_from_name(name=science_mss[0].path), CASDANameComponents
    ):
        preprocessed_mss = task_copy_and_preprocess_casda_askap_ms.map(
            casda_ms=science_mss,
            casa_container=field_options.casa_container,
            output_directory=output_path,
        )
        if field_options.flagger_container is not None:
            preprocessed_mss = task_flag_ms_aoflagger.map(  # type: ignore
                ms=preprocessed_mss, container=field_options.flagger_container
            )
            if field_options.casda_bandpass_table is not None:
                preprocessed_mss = task_flag_antenna_from_casda_bandpass_table.map(
                    ms=preprocessed_mss,
                    bandpass_table=field_options.casda_bandpass_table,
                )
        return preprocessed_mss

    # TODO: This will likely need to be expanded should any
    # other calibration strategies get added
    # Scan the existing bandpass directory for the existing solutions
    assert bandpass_path, f"{bandpass_path=}, it needs to be set"

    calibrate_cmds = find_existing_solutions(
        bandpass_directory=bandpass_path,
        use_preflagged=field_options.use_preflagger,
        use_smoothed=field_options.use_smoothed,
    )

    logger.info(f"Constructed the following {calibrate_cmds=}")

    split_science_mss = task_split_by_field.map(
        ms=science_mss,
        field=None,
        out_dir=unmapped(output_path),
        column=unmapped("DATA"),
    )

    # This will block until resolved
    flat_science_mss = task_flatten.submit(split_science_mss).result()

    solutions_paths = task_select_solution_for_ms.map(
        calibrate_cmds=unmapped(calibrate_cmds), ms=flat_science_mss
    )
    apply_solutions_cmds = task_create_apply_solutions_cmd.map(
        ms=flat_science_mss,
        solutions_file=solutions_paths,
        container=field_options.calibrate_container,
    )

    flagged_mss = task_flag_ms_aoflagger.map(
        ms=apply_solutions_cmds, container=field_options.flagger_container
    )
    column_rename_mss = task_rename_column_in_ms.map(
        ms=flagged_mss,
        original_column_name=unmapped("DATA"),
        new_column_name=unmapped("INSTRUMENT_DATA"),
    )
    return task_preprocess_askap_ms.map(
        ms=column_rename_mss,
        data_column=unmapped("CORRECTED_DATA"),
        instrument_column=unmapped("DATA"),
        overwrite=True,
    )


def image_mss(
    mss: Sequence[PrefectFuture[Any]],
    field_options: FieldOptions,
    strategy: Strategy | None,
    current_round: int,
    fits_masks: Sequence[PrefectFuture[FITSMaskNames]] | None = None,
) -> list[PrefectFuture[WSCleanResult]]:
    """Image each MS with wsclean using the options of a self-calibration round."""
    wsclean_results = task_wsclean_imager.map(
        in_ms=mss,
        wsclean_container=field_options.wsclean_container,
        fits_mask=fits_masks,
        update_wsclean_options=unmapped(
            get_options_from_strategy(
                strategy=strategy,
                mode="wsclean",
                round_info=current_round,
                operation="selfcal",
            )
        ),
        update_fitscube_options=unmapped(
            get_selfcal_round_fitscube_options(
                strategy=strategy,
                operation="selfcal",
                current_round=current_round,
                final_round=current_round == field_options.rounds,
            )
        ),
    )
    if not field_options.update_model_data_with_source_list:
        return wsclean_results

    return task_add_model_source_list_to_ms.map(
        wsclean_command=wsclean_results,
        calibrate_container=field_options.calibrate_container,
    )


@flow(name="Flint Continuum Pipeline")
def process_science_fields(
    science_path: Path,
    split_path: Path,
    field_options: FieldOptions,
    bandpass_path: Path | None = None,
) -> list[PrefectFuture[Any]]:
    # returned futures are resolved by prefect to fail the flow on task failure

    # Verify no nasty incompatible options
    _check_field_options(field_options=field_options)

    # Get some placeholder names
    run_aegean = (
        False if field_options.aegean_container is None else field_options.run_aegean
    )  # This is also in check_field_options

    science_mss = find_mss(
        mss_parent_path=science_path, expected_ms_count=field_options.expected_ms
    )

    output_split_science_path = _check_create_output_split_science_path(
        science_path=science_path, split_path=split_path, check_exists=True
    )

    dump_field_options_to_yaml(
        output_path=add_timestamp_to_path(
            input_path=output_split_science_path / "field_options.yaml"
        ),
        field_options=field_options,
    )

    archive_wait_for: list[PrefectFuture[Any]] = []

    strategy: Strategy | None = load_and_copy_strategy(
        output_split_science_path=output_split_science_path,
        imaging_strategy=field_options.imaging_strategy,
    )

    logger.info(f"{field_options=}")

    logger.info(f"Found the following raw measurement sets: {science_mss}")

    mss = preprocess_science_mss(
        science_mss=science_mss,
        field_options=field_options,
        output_path=output_split_science_path,
        bandpass_path=bandpass_path,
    )

    if field_options.no_imaging:
        logger.info(
            f"No imaging will be performed, as requested by {field_options.no_imaging=}"
        )
        return list(mss)

    if field_options.wsclean_container is None:
        logger.info("No wsclean container provided. Returning. ")
        return list(mss)

    if field_options.potato_container:
        # The call into potato peel task has two potential update option keywords.
        # So for the moment we will not use the task decorated version.
        potato_wsclean_init = get_options_from_strategy(
            strategy=strategy, mode="wsclean", round_info=0, operation="selfcal"
        )
        potato_peel_options = get_options_from_strategy(
            strategy=strategy, mode="potatopeel", round_info=0, operation="selfcal"
        )
        mss = task_potato_peel.map(
            ms=mss,
            potato_container=field_options.potato_container,
            update_wsclean_options=unmapped(potato_wsclean_init),
            update_potato_peel_options=unmapped(potato_peel_options),
        )

    if field_options.use_jolly_tukey_tractor:
        # TODO: Need to consider expanding the task below to
        # handle MS column?
        tukey_tractor_options = get_options_from_strategy(
            strategy=strategy, mode="tukeytractor", round_info=0, operation="selfcal"
        )
        mss = task_jolly_roger_tractor.map(
            ms=mss,
            update_tukey_tractor_options=unmapped(tukey_tractor_options),
        )

    if field_options.run_skymodel_calibration:
        with tags("skymodel-cal"):
            mss = calibrate_mss_against_sky_model(
                mss=mss,
                holofiles=[field_options.holofile] * len(mss),
                strategy=strategy,
                field_options=field_options,
            )

    # Summarised in each MS's own chain, as self-calibration later renames or
    # zips the MSs. The field and beam summaries are built from these so that
    # they never read an MS, and no beam has to wait on another
    ms_summaries = task_describe_ms.map(ms=mss, attach_ms=True)

    # All imaging and self-calibration is submitted before any convolution or
    # linmos, so that the dask scheduler favours it
    imaging_results: dict[int, list[PrefectFuture[WSCleanResult]]] = {
        0: image_mss(
            mss=ms_summaries,
            field_options=field_options,
            strategy=strategy,
            current_round=0,
        )
    }
    archive_wait_for.extend(imaging_results[0])

    beam_summaries = task_create_beam_summary.map(
        ms_summary=ms_summaries, image_set=imaging_results[0]
    )

    beam_aegean_outputs = None
    if run_aegean:
        beam_aegean_outputs = task_run_bane_and_aegean.map(
            image=imaging_results[0],
            aegean_container=unmapped(field_options.aegean_container),
            update_bane_options=unmapped(
                get_options_from_strategy(
                    strategy=strategy, mode="bane", round_info=0, operation="selfcal"
                )
            ),
            update_aegean_options=unmapped(
                get_options_from_strategy(
                    strategy=strategy, mode="aegean", round_info=0, operation="selfcal"
                )
            ),
        )
        beam_summaries = task_update_with_options.map(
            input_object=beam_summaries, components=beam_aegean_outputs
        )
    archive_wait_for.extend(beam_summaries)

    field_summary = task_create_field_summary.submit(
        mss=None,  # Constructed from the ms summaries
        cal_sbid_path=bandpass_path,
        holography_path=field_options.holofile,
        ms_summaries=ms_summaries,
    )
    field_summary = task_update_with_options.submit(
        input_object=field_summary, beam_summaries=beam_summaries
    )

    # Set up the default value should the user activated mask option is not set
    fits_beam_masks = None

    for current_round in range(1, field_options.rounds + 1):
        with tags(f"selfcal-{current_round}"):
            previous_results = imaging_results[current_round - 1]

            skip_gaincal_current_round = consider_skip_selfcal_on_round(
                current_round=current_round,
                skip_selfcal_on_rounds=field_options.skip_selfcal_on_rounds,
            )
            update_gain_options = get_options_from_strategy(
                strategy=strategy,
                mode="gaincal",
                round_info=current_round,
                operation="selfcal",
            )
            # TODO: Offer DP3 (task_dp3_solve_apply) as a container-free self-calibration backend
            cal_mss = task_gaincal_applycal_ms.map(
                ms=previous_results,
                selfcal_round=current_round,
                archive_input_ms=field_options.zip_ms,
                skip_selfcal=skip_gaincal_current_round,
                rename_ms=field_options.rename_ms,
                archive_cal_table=True,
                casa_container=field_options.casa_container,
                update_gain_cal_options=unmapped(update_gain_options),
            )  # type: ignore

            fits_beam_masks = None
            if consider_beam_mask_round(
                current_round=current_round,
                mask_rounds=(
                    field_options.use_beam_masks_rounds
                    if field_options.use_beam_masks_rounds
                    else field_options.use_beam_masks_from
                ),
                allow_beam_masks=field_options.use_beam_masks,
            ):
                # Early versions of the masking procedure required aegean outputs
                # to construct the signal images. Since aegean is run on the round 0
                # images already, they are reused on the first round
                # TODO: the aegean outputs are only needed should
                # the signal image be needed or per beam sources are needed
                if current_round >= 2 or beam_aegean_outputs is None:
                    beam_aegean_outputs = task_run_bane_and_aegean.map(
                        image=previous_results,
                        aegean_container=unmapped(field_options.aegean_container),
                        update_bane_options=unmapped(
                            get_options_from_strategy(
                                strategy=strategy,
                                mode="bane",
                                round_info=current_round,
                                operation="selfcal",
                            )
                        ),
                        update_aegean_options=unmapped(
                            get_options_from_strategy(
                                strategy=strategy,
                                mode="aegean",
                                round_info=current_round,
                                operation="selfcal",
                            )
                        ),
                    )

                update_masking_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="masking",
                    round_info=current_round,
                    operation="selfcal",
                )
                fits_beam_masks = task_create_image_mask_model.map(
                    image=previous_results,
                    image_products=beam_aegean_outputs,
                    update_masking_options=unmapped(update_masking_options),
                )  # type: ignore

            imaging_results[current_round] = image_mss(
                mss=cal_mss,
                field_options=field_options,
                strategy=strategy,
                current_round=current_round,
                fits_masks=fits_beam_masks,
            )
            archive_wait_for.extend(imaging_results[current_round])

    final_results = imaging_results[field_options.rounds]

    stokes_v_results = None
    if field_options.stokes_v_imaging:
        with tags("stokes-v"):
            # Imaging through the Stokes-I results means each final MS is only
            # imaged once its Stokes-I imaging is done
            stokes_v_results = task_wsclean_imager.map(
                in_ms=final_results,
                wsclean_container=field_options.wsclean_container,
                update_wsclean_options=unmapped(
                    get_options_from_strategy(
                        strategy=strategy, mode="wsclean", operation="stokesv"
                    )
                ),
                fits_mask=fits_beam_masks,
                update_fitscube_options=unmapped(
                    get_options_from_strategy(
                        strategy=strategy, mode="fitscube", operation="stokesv"
                    )
                ),
            )
            archive_wait_for.extend(stokes_v_results)

    # Do source finding on the last round of self-cal'ed images
    if run_aegean and field_options.rounds > 0:
        archive_wait_for.extend(
            task_run_bane_and_aegean.map(
                image=final_results,
                aegean_container=unmapped(field_options.aegean_container),
            )
        )

    if field_options.yandasoft_container:
        for current_round, wsclean_results in imaging_results.items():
            parsets = create_convol_linmos_images(
                wsclean_results=wsclean_results,
                field_options=field_options,
                field_summary=field_summary,
                # indicate in output linmos name no selfcal
                additional_linmos_suffix_str="noselfcal"
                if current_round == 0
                else None,
            )
            archive_wait_for.extend(parsets)

            if not run_aegean or current_round not in (0, field_options.rounds):
                continue

            aegean_outputs = task_run_bane_and_aegean.submit(
                image=parsets[-1], aegean_container=field_options.aegean_container
            )  # type: ignore
            field_summary = task_update_field_summary.submit(
                field_summary=field_summary,
                aegean_outputs=aegean_outputs,
                linmos_command=parsets[-1],
                round=current_round if current_round > 0 else None,
            )  # type: ignore
            archive_wait_for.append(field_summary)

            val_results = validation_items(
                field_summary=field_summary,
                aegean_outputs=aegean_outputs,
                reference_catalogue_directory=field_options.reference_catalogue_directory,
            )
            if val_results:
                archive_wait_for.extend(val_results)

        if stokes_v_results is not None:
            archive_wait_for.extend(
                create_convol_linmos_images(
                    wsclean_results=stokes_v_results,
                    field_options=field_options.with_options(linmos_residuals=False),
                    field_summary=field_summary,
                )
            )

    if field_options.zip_ms:
        # Stokes-V imaging, when run, is the last to read each final MS
        archive_wait_for.extend(
            task_zip_ms.map(
                in_item=final_results if stokes_v_results is None else stokes_v_results
            )
        )

    # There are some blocking calls in the cube coadding, so do it
    # as late as possible
    if field_options.coadd_cubes:
        final_round_fitscube_options = get_options_from_strategy(
            strategy=strategy,
            operation="selfcal",
            mode="fitscube",
            round_info=field_options.rounds,
        )
        with tags("cubes"):
            archive_wait_for.extend(
                create_convolve_linmos_cubes(
                    wsclean_results=final_results,  # type: ignore
                    field_options=field_options,
                    current_round=field_options.rounds or None,
                    additional_linmos_suffix_str="cube",
                    fitscube_options=FitsCubeOptions().with_options(
                        **final_round_fitscube_options
                    ),
                )
            )

    if field_options.sbid_archive_path or field_options.sbid_copy_path:
        update_archive_options = get_options_from_strategy(
            strategy=strategy, mode="archive", round_info=0, operation="selfcal"
        )
        archive_sbid = task_archive_sbid.submit(
            science_folder_path=output_split_science_path,
            archive_path=field_options.sbid_archive_path,
            copy_path=field_options.sbid_copy_path,
            max_round=field_options.rounds or None,
            update_archive_options=update_archive_options,
            wait_for=archive_wait_for,
        )
        archive_wait_for.append(archive_sbid)

    return archive_wait_for


def setup_run_process_science_field(
    cluster_config: str | Path,
    science_path: Path,
    split_path: Path,
    field_options: FieldOptions,
    bandpass_path: Path | None = None,
) -> None:
    science_sbid = get_sbid_from_path(path=science_path)

    if field_options.sbid_copy_path:
        updated_sbid_copy_path = field_options.sbid_copy_path / f"{science_sbid}"
        logger.info(f"Updating archive copy path to {updated_sbid_copy_path=}")
        field_options = field_options.with_options(
            sbid_copy_path=updated_sbid_copy_path
        )

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_science_fields.with_options(
        name=f"Flint Continuum Pipeline - {science_sbid}", task_runner=dask_task_runner
    )(
        science_path=science_path,
        bandpass_path=bandpass_path,
        split_path=split_path,
        field_options=field_options,
    )

    # TODO: Put the archive stuff here?


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )

    parser.add_argument(
        "science_path",
        type=Path,
        help="Path to directories containing the beam-wise science measurementsets that will have solutions copied over and applied.",
    )
    parser.add_argument(
        "--split-path",
        type=Path,
        default=Path("."),
        help="Location to write field-split MSs to. Will attempt to use the parent name of a directory when writing out a new MS. ",
    )
    parser.add_argument(
        "--calibrated-bandpass-path",
        type=Path,
        default=None,
        help="Path to directory containing the uncalibrated beam-wise measurement sets that contain the bandpass calibration source. If None then the '--sky-model-directory' should be provided. ",
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(parser=parser, options_class=FieldOptions)

    return parser


def cli() -> None:
    import logging

    # logger = logging.getLogger("flint")
    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    field_options: FieldOptions = create_options_from_parser(
        parser_namespace=args,
        options_class=FieldOptions,
    )

    setup_run_process_science_field(
        cluster_config=args.cluster_config,
        science_path=args.science_path,
        bandpass_path=args.calibrated_bandpass_path,
        split_path=args.split_path,
        field_options=field_options,
    )


if __name__ == "__main__":
    cli()
