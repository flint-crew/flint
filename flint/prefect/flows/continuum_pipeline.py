"""A prefect based pipeline that:
- will perform bandpass calibration with PKS B1934-638 data, or from a derived sky-model
- copy and apply to science field
- image and self-calibration the science fields
- run aegean source finding
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from configargparse import ArgumentParser
from prefect import flow, tags, unmapped

from flint.calibrate.aocalibrate import find_existing_solutions
from flint.catalogue import verify_reference_catalogues
from flint.coadd.linmos import LinmosResult
from flint.configuration import (
    Strategy,
    get_options_from_strategy,
    load_and_copy_strategy,
)
from flint.logging import logger
from flint.masking import consider_beam_mask_round
from flint.ms import find_mss
from flint.naming import (
    CASDANameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    FieldOptions,
    add_options_to_parser,
    create_options_from_parser,
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
from flint.prefect.common.ms import task_add_model_source_list_to_ms
from flint.prefect.common.utils import (
    task_archive_sbid,
    task_create_beam_summary,
    task_create_field_summary,
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


@flow(name="Flint Continuum Pipeline")
def process_science_fields(
    science_path: Path,
    split_path: Path,
    field_options: FieldOptions,
    bandpass_path: Path | None = None,
) -> None:
    # Verify no nasty incompatible options
    _check_field_options(field_options=field_options)

    # Get some placeholder names
    run_aegean = (
        False if field_options.aegean_container is None else field_options.run_aegean
    )  # This is also in check_field_options
    run_validation = field_options.reference_catalogue_directory is not None

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

    archive_wait_for: list[Any] = []

    strategy: Strategy | None = load_and_copy_strategy(
        output_split_science_path=output_split_science_path,
        imaging_strategy=field_options.imaging_strategy,
    )

    logger.info(f"{field_options=}")

    logger.info(f"Found the following raw measurement sets: {science_mss}")

    # TODO: This feels a little too much like that feeling of being out
    # at sea for to long. Should refactor (or mask a EMU only).
    if isinstance(
        extract_components_from_name(name=science_mss[0].path), CASDANameComponents
    ):
        preprocess_science_mss = task_copy_and_preprocess_casda_askap_ms.map(
            casda_ms=science_mss, output_directory=output_split_science_path
        )
        preprocess_science_mss = task_flag_ms_aoflagger.map(  # type: ignore
            ms=preprocess_science_mss, container=field_options.flagger_container
        )
    else:
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
            out_dir=unmapped(output_split_science_path),
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
        preprocess_science_mss = task_preprocess_askap_ms.map(
            ms=column_rename_mss,
            data_column=unmapped("CORRECTED_DATA"),
            instrument_column=unmapped("DATA"),
            overwrite=True,
        )

    if field_options.no_imaging:
        logger.info(
            f"No imaging will be performed, as requested by {field_options.no_imaging=}"
        )
        return

    field_summary = task_create_field_summary.submit(
        mss=preprocess_science_mss,
        cal_sbid_path=bandpass_path,
        holography_path=field_options.holofile,
    )  # type: ignore
    logger.info(f"{field_summary=}")

    if field_options.wsclean_container is None:
        logger.info("No wsclean container provided. Rerutning. ")
        return

    if field_options.potato_container:
        # The call into potato peel task has two potential update option keywords.
        # So for the moment we will not use the task decorated version.
        potato_wsclean_init = get_options_from_strategy(
            strategy=strategy, mode="wsclean", round_info=0, operation="selfcal"
        )
        preprocess_science_mss = task_potato_peel.map(
            ms=preprocess_science_mss,
            potato_container=field_options.potato_container,
            update_wsclean_options=unmapped(potato_wsclean_init),
        )

    stokes_v_mss = preprocess_science_mss
    wsclean_results = task_wsclean_imager.map(
        in_ms=preprocess_science_mss,
        wsclean_container=field_options.wsclean_container,
        update_wsclean_options=unmapped(
            get_options_from_strategy(
                strategy=strategy,
                mode="wsclean",
                round_info=0,
                operation="selfcal",
            )
        ),
    )  # type: ignore

    wsclean_results = (
        task_add_model_source_list_to_ms.map(
            wsclean_command=wsclean_results,
            calibrate_container=field_options.calibrate_container,
        )
        if field_options.update_model_data_with_source_list
        else wsclean_results
    )

    beam_summaries = task_create_beam_summary.map(
        ms=preprocess_science_mss, image_set=wsclean_results
    )
    archive_wait_for.extend(beam_summaries)
    archive_wait_for.extend(wsclean_results)

    beam_aegean_outputs = None
    if run_aegean:
        update_bane_options = get_options_from_strategy(
            strategy=strategy,
            mode="bane",
            round_info=0,
            operation="selfcal",
        )
        update_aegean_options = get_options_from_strategy(
            strategy=strategy,
            mode="aegean",
            round_info=0,
            operation="selfcal",
        )
        beam_aegean_outputs = task_run_bane_and_aegean.map(
            image=wsclean_results,
            aegean_container=unmapped(field_options.aegean_container),
            update_bane_options=unmapped(update_bane_options),
            update_aegean_options=unmapped(update_aegean_options),
        )
        beam_summaries = task_update_with_options.map(
            input_object=beam_summaries, components=beam_aegean_outputs
        )
        field_summary = task_update_with_options.submit(
            input_object=field_summary, beam_summaries=beam_summaries
        )

    if field_options.yandasoft_container:
        parsets = create_convol_linmos_images(
            wsclean_results=wsclean_results,
            field_options=field_options,
            field_summary=field_summary,
            additional_linmos_suffix_str="noselfcal",  # indicate in output linmos name no selfcal
        )
        archive_wait_for.extend(parsets)
        parset = parsets[-1]

        if run_aegean:
            aegean_field_output = task_run_bane_and_aegean.submit(
                image=parset, aegean_container=unmapped(field_options.aegean_container)
            )  # type: ignore
            field_summary = task_update_field_summary.submit(
                field_summary=field_summary,
                aegean_outputs=aegean_field_output,
                linmos_command=parset,
            )  # type: ignore
            archive_wait_for.append(field_summary)

            if run_validation and field_options.reference_catalogue_directory:
                validation_items(
                    field_summary=field_summary,
                    aegean_outputs=aegean_field_output,
                    reference_catalogue_directory=field_options.reference_catalogue_directory,
                )

    # Set up the default value should the user activated mask option is not set
    fits_beam_masks = None

    for current_round in range(1, field_options.rounds + 1):
        with tags(f"selfcal-{current_round}"):
            final_round = current_round == field_options.rounds

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
            cal_mss = task_gaincal_applycal_ms.map(
                ms=wsclean_results,
                selfcal_round=current_round,
                archive_input_ms=field_options.zip_ms,
                skip_selfcal=skip_gaincal_current_round,
                rename_ms=field_options.rename_ms,
                archive_cal_table=True,
                casa_container=field_options.casa_container,
                update_gain_cal_options=unmapped(update_gain_options),
                wait_for=[
                    field_summary,
                    # To make sure field summary is created with unzipped MSs
                    *beam_summaries,
                    # to make sure gaincal does not run before the beam summary
                ],
            )  # type: ignore
            stokes_v_mss = cal_mss

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
                # TODO: the aegean outputs are only needed should
                # the signal image be needed or per beam sources are needed
                update_bane_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="bane",
                    round_info=current_round,
                    operation="selfcal",
                )
                update_aegean_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="aegean",
                    round_info=current_round,
                    operation="selfcal",
                )

                beam_aegean_outputs = (
                    task_run_bane_and_aegean.map(
                        image=wsclean_results,
                        aegean_container=unmapped(field_options.aegean_container),
                        update_bane_options=unmapped(update_bane_options),
                        update_aegean_options=unmapped(update_aegean_options),
                    )
                    if (current_round >= 2 or not beam_aegean_outputs)
                    else beam_aegean_outputs
                )
                # Early versions of the masking procedure required aegean outputs
                # to construct the sginal images. Since aegean is run outside of
                # this self-cal loop once already, we can skip their creation on
                # the first loop

                update_masking_options = get_options_from_strategy(
                    strategy=strategy,
                    mode="masking",
                    round_info=current_round,
                    operation="selfcal",
                )
                fits_beam_masks = task_create_image_mask_model.map(
                    image=wsclean_results,
                    image_products=beam_aegean_outputs,
                    update_masking_options=unmapped(update_masking_options),
                )  # type: ignore

            update_wsclean_options = get_options_from_strategy(
                strategy=strategy,
                mode="wsclean",
                operation="selfcal",
                round_info=current_round,
            )
            wsclean_results = task_wsclean_imager.map(
                in_ms=cal_mss,
                wsclean_container=field_options.wsclean_container,
                fits_mask=fits_beam_masks,
                update_wsclean_options=unmapped(update_wsclean_options),
            )
            wsclean_results = (
                task_add_model_source_list_to_ms.map(
                    wsclean_command=wsclean_results,
                    calibrate_container=field_options.calibrate_container,
                )
                if field_options.update_model_data_with_source_list
                else wsclean_results
            )
            archive_wait_for.extend(wsclean_results)

            # Do source finding on the last round of self-cal'ed images
            if round == field_options.rounds and run_aegean:
                task_run_bane_and_aegean.map(
                    image=wsclean_results,
                    aegean_container=unmapped(field_options.aegean_container),
                )

            parsets_self: None | list[LinmosResult] = None  # Without could be unbound
            if field_options.yandasoft_container:
                parsets_self = create_convol_linmos_images(
                    wsclean_results=wsclean_results,
                    field_options=field_options,
                    field_summary=field_summary,
                )
                archive_wait_for.extend(parsets_self)

            if final_round and run_aegean and parsets_self:
                aegean_outputs = task_run_bane_and_aegean.submit(
                    image=parsets_self[-1],
                    aegean_container=unmapped(field_options.aegean_container),
                )  # type: ignore
                field_summary = task_update_field_summary.submit(
                    field_summary=field_summary,
                    aegean_outputs=aegean_outputs,
                    round=current_round,
                )  # type: ignore
                if run_validation:
                    assert field_options.reference_catalogue_directory, (
                        f"Reference catalogue directory should be set when {run_validation=}"
                    )
                    val_results = validation_items(
                        field_summary=field_summary,
                        aegean_outputs=aegean_outputs,
                        reference_catalogue_directory=field_options.reference_catalogue_directory,
                    )
                    archive_wait_for.append(val_results)

    if field_options.coadd_cubes:
        with tags("cubes"):
            cube_parset = create_convolve_linmos_cubes(
                wsclean_results=wsclean_results,  # type: ignore
                field_options=field_options,
                current_round=(field_options.rounds if field_options.rounds else None),
                additional_linmos_suffix_str="cube",
            )
            archive_wait_for.append(cube_parset)

    if field_options.stokes_v_imaging:
        with tags("stokes-v"):
            stokes_v_wsclean_options = get_options_from_strategy(
                strategy=strategy, mode="wsclean", operation="stokesv"
            )
            wsclean_results = task_wsclean_imager.map(
                in_ms=stokes_v_mss,
                wsclean_container=field_options.wsclean_container,
                update_wsclean_options=unmapped(stokes_v_wsclean_options),
                fits_mask=fits_beam_masks,
                wait_for=wsclean_results,  # Ensure that measurement sets are doubled up during imaging
            )
            if field_options.yandasoft_container:
                parsets = create_convol_linmos_images(
                    wsclean_results=wsclean_results,
                    field_options=field_options.with_options(linmos_residuals=False),
                    field_summary=field_summary,
                )
                archive_wait_for.extend(parsets)

    # zip up the final measurement set, which is not included in the above loop
    if field_options.zip_ms:
        archive_wait_for = task_zip_ms.map(
            in_item=wsclean_results, wait_for=archive_wait_for
        )

    if field_options.sbid_archive_path or field_options.sbid_copy_path:
        update_archive_options = get_options_from_strategy(
            strategy=strategy, mode="archive", round_info=0, operation="selfcal"
        )
        task_archive_sbid.submit(
            science_folder_path=output_split_science_path,
            archive_path=field_options.sbid_archive_path,
            copy_path=field_options.sbid_copy_path,
            max_round=field_options.rounds if field_options.rounds else None,
            update_archive_options=update_archive_options,
            wait_for=archive_wait_for,
        )


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
