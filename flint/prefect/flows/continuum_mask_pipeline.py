"""A prefect based pipeline that:
- will perform bandpass calibration with PKS B1934-638 data, or from a derived sky-model
- copy and apply to science field
- image and self-calibration the science fields
- run aegean source finding

This pipeline will attempt to incorporate a masking operation once a linmos
field has been produced. Given a linmos image, a signal and mask operation
will create a large field image that could then be used as a clean mask
provided to wsclean. This process would require a mask to be extracted from
the larger signal linmos mask image using a template WCS header. It seems
that the best way to do this would be to use a header from the previous
imaging round.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from prefect import flow, unmapped

from flint.calibrate.aocalibrate import find_existing_solutions
from flint.logging import logger
from flint.ms import MS
from flint.naming import get_sbid_from_path
from flint.options import FieldOptions
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    _convolve_linmos_residuals,
    task_convolve_image,
    task_create_apply_solutions_cmd,
    task_create_linmos_mask_model,
    task_create_linmos_mask_wbutter_model,
    task_create_validation_plot,
    task_create_validation_tables,
    task_extract_beam_mask_image,
    task_flag_ms_aoflagger,
    task_gaincal_applycal_ms,
    task_get_common_beam,
    task_linmos_images,
    task_preprocess_askap_ms,
    task_run_bane_and_aegean,
    task_select_solution_for_ms,
    task_split_by_field,
    task_wsclean_imager,
    task_zip_ms,
)
from flint.prefect.common.utils import task_flatten


def _create_linmos_mask(
    linmos_parset: Any, aegean_outputs: Any, butterworth_filter: bool
) -> Any:
    """A common function that pulls together the two linmos masking operations.
    This is an internal function that is calling prefect tasks. It itself can not be
    @task, and is intended not to be share among other codes.
    """
    # At the moment no others tasks or flows should use this, ya filthy pirate.
    if butterworth_filter:
        linmos_mask = task_create_linmos_mask_wbutter_model(
            linmos_parset=linmos_parset,
            image_products=aegean_outputs,
        )
    else:
        linmos_mask = task_create_linmos_mask_model(
            linmos_parset=linmos_parset,
            image_products=aegean_outputs,
        )

    return linmos_mask


@flow(name="Flint Continuum Pipeline")
def process_science_fields(
    science_path: Path,
    bandpass_path: Path,
    split_path: Path,
    field_options: FieldOptions,
) -> None:
    run_aegean = (
        False if field_options.aegean_container is None else field_options.run_aegean
    )
    run_validation = field_options.reference_catalogue_directory is not None

    assert (
        science_path.exists() and science_path.is_dir()
    ), f"{science_path!s} does not exist or is not a folder. "
    science_mss = list(
        [MS.cast(ms_path) for ms_path in sorted(science_path.glob("*.ms"))]
    )
    assert (
        len(science_mss) == field_options.expected_ms
    ), f"Expected to find {field_options.expected_ms} in {science_path!s}, found {len(science_mss)}."

    science_folder_name = science_path.name

    output_split_science_path = (
        Path(split_path / science_folder_name).absolute().resolve()
    )

    if not output_split_science_path.exists():
        logger.info(f"Creating {output_split_science_path!s}")
        output_split_science_path.mkdir(parents=True)

    logger.info(f"Found the following raw measurement sets: {science_mss}")

    # TODO: This will likely need to be expanded should any
    # other calibration strategies get added
    # Scan the existing bandpass directory for the existing solutions
    calibrate_cmds = find_existing_solutions(
        bandpass_directory=bandpass_path, use_preflagged=True, use_smoothed=True
    )

    logger.info(f"Constructed the following {calibrate_cmds=}")

    split_science_mss = task_split_by_field.map(
        ms=science_mss, field=None, out_dir=unmapped(output_split_science_path)
    )
    flat_science_mss = task_flatten.submit(split_science_mss)

    preprocess_science_mss = task_preprocess_askap_ms.map(
        ms=flat_science_mss,
        data_column=unmapped("DATA"),
        instrument_column=unmapped("INSTRUMENT_DATA"),
        overwrite=True,
    )
    solutions_paths = task_select_solution_for_ms.map(
        calibrate_cmds=unmapped(calibrate_cmds), ms=preprocess_science_mss
    )
    apply_solutions_cmds = task_create_apply_solutions_cmd.map(
        ms=preprocess_science_mss,
        solutions_file=solutions_paths,
        container=field_options.calibrate_container,
    )
    flagged_mss = task_flag_ms_aoflagger.map(
        ms=apply_solutions_cmds, container=field_options.flagger_container, rounds=1
    )

    if field_options.no_imaging:
        logger.info(
            f"No imaging will be performed, as requested by {field_options.no_imaging=}"
        )
        return

    if field_options.wsclean_container is None:
        logger.info("No wsclean container provided. Rerutning. ")
        return

    wsclean_init = {
        "size": 9000,
        "minuv_l": 235,
        "weight": "briggs -1.0",
        "auto_mask": 4,
        "multiscale": True,
        "local_rms_window": 55,
        "multiscale_scales": (0, 15, 30, 40, 50, 60),
        "multiscale_scale_bias": 0.75,
        "niter": 750000 * 3,
        "channels_out": 8,
    }

    wsclean_cmds = task_wsclean_imager.map(
        in_ms=flagged_mss,
        wsclean_container=field_options.wsclean_container,
        update_wsclean_options=unmapped(wsclean_init),
    )

    task_run_bane_and_aegean.map(
        image=wsclean_cmds, aegean_container=unmapped(field_options.aegean_container)
    )

    beam_shape = task_get_common_beam.submit(wsclean_cmds=wsclean_cmds, cutoff=150.0)
    conv_images = task_convolve_image.map(
        wsclean_cmd=wsclean_cmds, beam_shape=unmapped(beam_shape), cutoff=150.0
    )
    parset = task_linmos_images.submit(
        images=conv_images,
        container=field_options.yandasoft_container,
        suffix_str="noselfcal",
        holofile=field_options.holofile,
    )

    if field_options.linmos_residuals:
        _convolve_linmos_residuals(
            wsclean_cmds=wsclean_cmds,
            beam_shape=beam_shape,
            field_options=field_options,
        )

    aegean_outputs = task_run_bane_and_aegean.submit(
        image=parset, aegean_container=unmapped(field_options.aegean_container)
    )

    linmos_mask = _create_linmos_mask(
        linmos_parset=parset,
        aegean_outputs=aegean_outputs,
        butterworth_filter=field_options.butterworth_filter,
    )

    beam_masks = task_extract_beam_mask_image.map(
        linmos_mask_names=unmapped(linmos_mask), wsclean_cmd=wsclean_cmds
    )

    if field_options.rounds is None:
        logger.info("No self-calibration will be performed. Returning")
        return

    gain_cal_rounds = {
        1: {"solint": "60s", "uvrange": ">235lambda", "nspw": 1},
        2: {"solint": "20s", "uvrange": ">235lambda", "nspw": 1},
    }
    wsclean_rounds = {
        1: {
            "size": 9000,
            "multiscale": True,
            "minuv_l": 235,
            "weight": "briggs -1.0",
            "auto_mask": 4,
            "local_rms_window": 110,
            "multiscale_scales": (0, 15, 30, 40, 50, 60),
            "multiscale_scale_bias": 0.75,
            "threshold": 0.00006,
            "niter": 750000 * 3,
            "channels_out": 8,
        },
        2: {
            "size": 9000,
            "multiscale": True,
            "minuv_l": 235,
            "weight": "briggs -1.0",
            "auto_mask": 2.0,
            "local_rms_window": 110,
            "multiscale_scales": (0, 15, 30, 40, 50, 60),
            "multiscale_scale_bias": 0.75,
            "threshold": 0.00006,
            "niter": 750000 * 3,
            "channels_out": 8,
        },
    }

    for round in range(1, field_options.rounds + 1):
        final_round = round == field_options.rounds

        gain_cal_options = gain_cal_rounds.get(round, None)
        wsclean_options = wsclean_rounds.get(round, None)

        cal_mss = task_gaincal_applycal_ms.map(
            wsclean_cmd=wsclean_cmds,
            round=round,
            update_gain_cal_options=unmapped(gain_cal_options),
            archive_input_ms=field_options.zip_ms,
        )

        flag_mss = task_flag_ms_aoflagger.map(
            ms=cal_mss, container=field_options.flagger_container, rounds=1
        )
        wsclean_cmds = task_wsclean_imager.map(
            in_ms=flag_mss,
            wsclean_container=field_options.wsclean_container,
            update_wsclean_options=unmapped(wsclean_options),
            fits_mask=beam_masks,
        )

        # Do source finding on the last round of self-cal'ed images
        if final_round and run_aegean:
            task_run_bane_and_aegean.map(
                image=wsclean_cmds,
                aegean_container=unmapped(field_options.aegean_container),
            )
            task_zip_ms.map(in_item=wsclean_cmds)

        beam_shape = task_get_common_beam.submit(
            wsclean_cmds=wsclean_cmds, cutoff=150.0
        )
        conv_images = task_convolve_image.map(
            wsclean_cmd=wsclean_cmds, beam_shape=unmapped(beam_shape), cutoff=150.0
        )
        if field_options.yandasoft_container is None:
            logger.info("No yandasoft container supplied, not linmosing. ")
            continue

        parset = task_linmos_images.submit(
            images=conv_images,
            container=field_options.yandasoft_container,
            suffix_str=f"round{round}",
            holofile=field_options.holofile,
        )

        aegean_outputs = task_run_bane_and_aegean.submit(
            image=parset, aegean_container=unmapped(field_options.aegean_container)
        )

        if final_round and field_options.linmos_residuals:
            _convolve_linmos_residuals(
                wsclean_cmds=wsclean_cmds,
                beam_shape=beam_shape,
                field_options=field_options,
            )

        # Use the mask from the first round
        if round < field_options.rounds:
            linmos_mask = _create_linmos_mask(
                linmos_parset=parset,
                aegean_outputs=aegean_outputs,
                butterworth_filter=field_options.butterworth_filter,
            )

            beam_masks = task_extract_beam_mask_image.map(
                linmos_mask_names=unmapped(linmos_mask), wsclean_cmd=wsclean_cmds
            )

        if run_validation:
            task_create_validation_plot.submit(
                processed_mss=flagged_mss,
                aegean_outputs=aegean_outputs,
                reference_catalogue_directory=field_options.reference_catalogue_directory,
            )
            task_create_validation_tables.submit(
                processed_mss=flagged_mss,
                aegean_outputs=aegean_outputs,
                reference_catalogue_directory=field_options.reference_catalogue_directory,
            )


def setup_run_process_science_field(
    cluster_config: str | Path,
    science_path: Path,
    bandpass_path: Path,
    split_path: Path,
    field_options: FieldOptions,
) -> None:
    assert (
        bandpass_path.exists() and bandpass_path.is_dir()
    ), f"{bandpass_path=} needs to exist and be a directory! "

    science_sbid = get_sbid_from_path(path=science_path)

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_science_fields.with_options(
        name=f"Flint Continuum Masked Pipeline - {science_sbid}",
        task_runner=dask_task_runner,
    )(
        science_path=science_path,
        bandpass_path=bandpass_path,
        split_path=split_path,
        field_options=field_options,
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "science_path",
        type=Path,
        help="Path to directories containing the beam-wise science measurementsets that will have solutions copied over and applied.",
    )
    parser.add_argument(
        "calibrated_bandpass_path",
        type=Path,
        default=None,
        help="Path to directory containing the calibrated beam-wise measurement sets that contain the bandpass calibration source and their solutions. ",
    )
    parser.add_argument(
        "--split-path",
        type=Path,
        default=Path("."),
        help="Location to write field-split MSs to. Will attempt to use the parent name of a directory when writing out a new MS. ",
    )

    parser.add_argument(
        "--holofile",
        type=Path,
        default=None,
        help="Path to the holography FITS cube used for primary beam corrections",
    )

    parser.add_argument(
        "--expected-ms",
        type=int,
        default=36,
        help="The expected number of measurement sets to find. ",
    )
    parser.add_argument(
        "--calibrate-container",
        type=Path,
        default="aocalibrate.sif",
        help="Path to container that holds AO calibrate and applysolutions. ",
    )
    parser.add_argument(
        "--flagger-container",
        type=Path,
        default="flagger.sif",
        help="Path to container with aoflagger software. ",
    )
    parser.add_argument(
        "--wsclean-container",
        type=Path,
        default=None,
        help="Path to the wsclean singularity container",
    )
    parser.add_argument(
        "--yandasoft-container",
        type=Path,
        default=None,
        help="Path to the singularity container with yandasoft",
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )
    parser.add_argument(
        "--selfcal-rounds",
        type=int,
        default=2,
        help="The number of selfcalibration rounds to perform. ",
    )
    parser.add_argument(
        "--zip-ms",
        action="store_true",
        help="Zip up measurement sets as imaging and self-calibration is carried out.",
    )
    parser.add_argument(
        "--run-aegean",
        action="store_true",
        help="Run the aegean source finder on images. ",
    )
    parser.add_argument(
        "--aegean-container",
        type=Path,
        default=None,
        help="Path to the singularity container with aegean",
    )
    parser.add_argument(
        "--no-imaging",
        action="store_true",
        help="Do not perform any imaging, only derive bandpass solutions and apply to sources. ",
    )
    parser.add_argument(
        "--reference-catalogue-directory",
        type=Path,
        default=None,
        help="Path to the directory containing the ICFS, NVSS and SUMSS reference catalogues. These are required for validation plots. ",
    )
    parser.add_argument(
        "--butterworth-filter",
        action="store_true",
        help="Apply a butterworth filter when creating a clean mask from the larger linmos image. This may help capture more extended diffuse emission. ",
    )
    parser.add_argument(
        "--linmos-residuals",
        action="store_true",
        default=False,
        help="Co-add the per-beam cleaning residuals into a field image",
    )

    return parser


def cli() -> None:
    import logging

    # logger = logging.getLogger("flint")
    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    field_options = FieldOptions(
        flagger_container=args.flagger_container,
        calibrate_container=args.calibrate_container,
        holofile=args.holofile,
        expected_ms=args.expected_ms,
        wsclean_container=args.wsclean_container,
        yandasoft_container=args.yandasoft_container,
        rounds=args.selfcal_rounds,
        zip_ms=args.zip_ms,
        run_aegean=args.run_aegean,
        aegean_container=args.aegean_container,
        no_imaging=args.no_imaging,
        reference_catalogue_directory=args.reference_catalogue_directory,
        butterworth_filter=args.butterworth_filter,
        linmos_residuals=args.linmos_residuals,
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
