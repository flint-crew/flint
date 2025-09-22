from __future__ import annotations

from pathlib import Path

from configargparse import ArgumentParser
from prefect import flow, tags, unmapped
from prefect.futures import PrefectFuture

from flint.coadd.linmos import LinmosOptions, LinmosResult
from flint.configuration import (
    POLARISATION_MAPPING,
    get_options_from_strategy,
    load_and_copy_strategy,
)
from flint.exceptions import MSError
from flint.imager.wsclean import (
    ImageSet,
    WSCleanResult,
)
from flint.logging import logger
from flint.ms import find_mss
from flint.naming import (
    CASDANameComponents,
    FITSMaskNames,
    ProcessedNameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    MS,
    PolFieldOptions,
    add_options_to_parser,
    create_options_from_parser,
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    task_combine_images_to_cube,
    task_convolve_images,
    task_get_channel_images_from_paths,
    task_get_common_beam_from_image_set,
    task_linmos_images,
    task_merge_image_sets,
    task_preprocess_askap_ms,
    task_split_and_get_image_set,
    task_wsclean_imager,
)
from flint.prefect.common.utils import (
    task_create_field_summary,
    task_create_object,
    task_getattr,
    task_rename_linear_to_stokes,
)


@flow(name="Flint Polarisation Pipeline")
def process_science_fields_pol(
    flint_ms_directory: Path,
    pol_field_options: PolFieldOptions,
    ms_is_flint: bool = False,
) -> None:
    strategy = load_and_copy_strategy(
        output_split_science_path=flint_ms_directory,
        imaging_strategy=pol_field_options.imaging_strategy,
    )

    logger.info(f"{pol_field_options=}")

    if strategy is None:
        logger.info("No strategy provided. Returning.")
        return

    # Get some placeholder names
    science_mss = list(
        find_mss(
            mss_parent_path=flint_ms_directory,
            expected_ms_count=pol_field_options.expected_ms,
            data_column=strategy["defaults"].get("data_column", "DATA"),
        )
    )

    if not ms_is_flint:
        # if user has not forced that MS is indeed from previous flint run, perform checks

        # Check if MSs have been processed by Flint or have been provided by CASDA
        from_flint_list = [
            isinstance(extract_components_from_name(ms.path), ProcessedNameComponents)
            for ms in science_mss
        ]
        from_casda_list = [
            isinstance(extract_components_from_name(ms.path), CASDANameComponents)
            for ms in science_mss
        ]

        if not any(from_flint_list) and not any(from_casda_list):
            raise MSError("No valid MeasurementSets found! Data must be calibrated first.")

        if any(from_flint_list) and any(from_casda_list):
            raise MSError("Cannot mix Flint-processed and CASDA-provided MeasurementSets!")

        if any(from_casda_list):
            assert all(from_casda_list), (
                "Some MeasurementSets are from Flint, some are from CASDA"
            )
            logger.info("Data are from CASDA, need to apply FixMS")
            if pol_field_options.casa_container is None:
                msg = "We need to apply FixMS to CASDA-provided data, but no CASA container provided"
                raise MSError(msg)

            corrected_mss = []
            for ms in science_mss:
                corrected_ms = task_preprocess_askap_ms.submit(
                    ms=ms,
                    data_column=strategy["defaults"].get("data_column", "DATA"),
                    skip_rotation=False,
                    fix_stokes_factor=True,
                    apply_ms_transform=True,
                    casa_container=pol_field_options.casa_container,
                    rename=True,
                )
                corrected_mss.append(corrected_ms)

            assert len(corrected_mss) == len(science_mss), (
                "Number of corrected MSs does not match number of input MSs"
            )
            science_mss = corrected_mss

    field_summary = task_create_field_summary.submit(
        mss=science_mss,
        holography_path=pol_field_options.holofile,
    )

    dump_field_options_to_yaml(
        output_path=add_timestamp_to_path(
            input_path=flint_ms_directory / "pol_field_options.yaml"
        ),
        field_options=pol_field_options,
    )

    logger.info(f"Found the following calibrated measurement sets: {science_mss}")

    if pol_field_options.wsclean_container is None:
        logger.info("No wsclean container provided. Returning. ")
        return

    polarisations: dict[str, str] = strategy.get("polarisation", {"total": {}})


    # fix typing
    image_sets_dict: dict[str, list[PrefectFuture[ImageSet]]] = {}
    image_sets_list: list[PrefectFuture[ImageSet]] = []

    # track the latest submitted job per MS so we can chain polarisation runs
    prev_by_ms: dict[MS, PrefectFuture[WSCleanResult]] = {}

    for polarisation in polarisations.keys():
        _image_sets: list[PrefectFuture[ImageSet]] = []

        with tags(f"polarisation-{polarisation}"):
            for science_ms in science_mss:
                # If we've already launched a job for this MS, make the next polarisation wait for it
                # WSClean can throw errors if two processes are accessing at the same time.
                upstream = [prev_by_ms[science_ms]] if science_ms in prev_by_ms else None

                if True:
                    # hardcode for testing. Assume the fitsmask is created with the same name as .ms
                    fits_mask_file = science_ms.path.name.replace(".ms", ".i.MFS.image.fits.mask.fits")
                    fits_mask_path = Path("/scratch/b/bmg/eosinga/doradogroup/production/688626888568920")
                    fits_mask_file = fits_mask_path / fits_mask_file

                    # create FITS mask per MS
                    fits_mask = FITSMaskNames(
                        mask_fits=fits_mask_file,
                        signal_fits=None
                    )
                else:
                    fits_mask = None

                wsclean_result: PrefectFuture[WSCleanResult] = task_wsclean_imager.submit(
                    in_ms=science_ms,
                    wsclean_container=pol_field_options.wsclean_container,
                    make_cube_from_subbands=False,      # we will do this later
                    recompute=False,                    # to not rerun wsclean jobs if some fraction of them already completed succesfully in an earlier run
                    ignore_tmpdir_files_with_globstr='tmp', # ADDED BY ERIK TO IGNORE MOVING WSCLEAN TMP FILES THAT SOMETIMES END UP IN THE MOVE_DIR FROM THE HOLD_DIR
                    file_exist_ok=True,                  # # ADDED BY ERIK BECAUSE HOLD_THEN_MOVE_INTO KEEPS FAILING FOR A RANDOM SUBSET OF JOBS BECAUSE FILES ALREADY EXIST.
                    fits_mask=fits_mask,                # <-- use the FITS mask we created above. TODO: make user input a fits_mask directory and a fits_mask glob
                    update_wsclean_options=unmapped(
                        get_options_from_strategy(
                            strategy=strategy,
                            operation="polarisation",
                            mode="wsclean",
                            polarisation=polarisation,
                        )
                    ),
                    wait_for=upstream,  # <-- serialize polarisations per MS, 
                                        # so that we do not run wsclean on the same MS in parallel
                )

                _image_set: PrefectFuture[ImageSet] = task_getattr.submit(wsclean_result, "image_set")
                _image_sets.append(_image_set)
                image_sets_list.append(_image_set)

                # Update the tail of the chain for this MS
                prev_by_ms[science_ms] = wsclean_result

        image_sets_dict[polarisation] = _image_sets

    merged_image_set = task_merge_image_sets.submit(image_sets=image_sets_list)

    common_beam_shape = task_get_common_beam_from_image_set.submit(
        image_set=merged_image_set,
        cutoff=pol_field_options.beam_cutoff,
        fixed_beam_shape=pol_field_options.fixed_beam_shape,
    )

    stokes_beam_cubes: dict[str, list[PrefectFuture[Path]]] = {}
    for polarisation, image_set_list in image_sets_dict.items():
        with tags(f"polarisation-{polarisation}"):
            # Get the individual Stokes parameters in case of joint imaging
            if polarisation not in POLARISATION_MAPPING.keys():
                raise ValueError(f"Unknown polarisation {polarisation}")
            stokes_list = list(POLARISATION_MAPPING[polarisation])
            for stokes in stokes_list:
                with tags(f"stokes-{stokes}"):
                    beam_cubes: list[PrefectFuture[Path]] = []
                    for image_set in image_set_list:
                        stokes_image_list = task_split_and_get_image_set.submit(
                            image_set=image_set,
                            get=stokes,
                            by="pol",
                            mode="image",
                        )
                        convolved_image_list = task_convolve_images.submit(
                            image_paths=stokes_image_list,
                            beam_shape=common_beam_shape,
                            cutoff=pol_field_options.beam_cutoff,
                        )
                        # TODO: Consider accerating this by doing a linmos per-channel, then combining
                        channel_image_list = task_get_channel_images_from_paths.submit(
                            paths=convolved_image_list
                        )
                        prefix = task_getattr.submit(image_set, "prefix")

                        if polarisation == "linear":
                            # Get single Stokes prefix - the original prefix is the linear prefix
                            # i.e. `.qu.` -> `.q.` or `.u.` depending on the stokes
                            prefix = task_rename_linear_to_stokes.submit(
                                linear_name=prefix,
                                stokes=stokes,
                            )
                        cube_path = task_combine_images_to_cube.submit(
                            images=channel_image_list,
                            prefix=prefix,
                            mode="image",
                            remove_original_images=True,
                        )

                        # # wait for all cube_path tasks for this polarisation to finish
                        # for cube_path_future in cube_path:
                        #     # this will block until that future completes
                        #     cube_path_future.result()

                        beam_cubes.append(cube_path)
                stokes_beam_cubes[stokes] = beam_cubes

    linmos_result_list: list[PrefectFuture[LinmosResult]] = []
    # We run linmos now to ensure we have Stokes I images for leakage correction
    # If we have not imaged Stokes I, we cannot do leakage correction
    force_remove_leakage: bool | None = None
    if "i" not in stokes_beam_cubes.keys():
        force_remove_leakage = False

    linmos_options = task_create_object(
        object=LinmosOptions,
        holofile=pol_field_options.holofile,
        cutoff=pol_field_options.pb_cutoff,
        stokesi_images=stokes_beam_cubes.get("i"),
        force_remove_leakage=force_remove_leakage,
        trim_linmos_fits=pol_field_options.trim_linmos_fits,
    )
    for stokes, beam_cubes in stokes_beam_cubes.items():
        with tags(f"stokes-{stokes}"):
            linmos_result = task_linmos_images.submit(
                image_list=beam_cubes,
                container=pol_field_options.yandasoft_container,
                linmos_options=linmos_options,
                field_summary=field_summary,
            )
            linmos_result_list.append(linmos_result)

    # wait for all linmos results to be completed
    _ = [linmos_result.result() for linmos_result in linmos_result_list]


def setup_run_process_science_field(
    cluster_config: str | Path,
    flint_ms_directory: Path,
    pol_field_options: PolFieldOptions,
    ms_is_flint: bool = False,
) -> None:
    science_sbid = get_sbid_from_path(path=flint_ms_directory)

    if pol_field_options.sbid_copy_path:
        updated_sbid_copy_path = pol_field_options.sbid_copy_path / f"{science_sbid}"
        logger.info(f"Updating archive copy path to {updated_sbid_copy_path=}")
        pol_field_options = pol_field_options.with_options(
            sbid_copy_path=updated_sbid_copy_path
        )

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_science_fields_pol.with_options(
        name=f"Flint Polarisation Pipeline - {science_sbid}",
        task_runner=dask_task_runner,
    )(
        flint_ms_directory=flint_ms_directory,
        pol_field_options=pol_field_options,
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )

    parser.add_argument(
        "flint_ms_directory",
        type=Path,
        help="Path to directories containing the beam-wise flint-calibrated MeasurementSets.",
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(
        parser=parser,
        options_class=PolFieldOptions,
        description="Polarisation processing options",
    )

    parser.add_argument(
        "--ms-is-flint",
        action="store_true",
        default=False,
        help="Overwrite automatic naming convention check. Assume that the MSs are flint calibrated. Default False",
    )    

    return parser


def cli() -> None:
    import logging

    # logger = logging.getLogger("flint")
    logger.setLevel(logging.INFO)

    parser = get_parser()

    args = parser.parse_args()

    field_options = create_options_from_parser(
        parser_namespace=args,
        options_class=PolFieldOptions,
    )

    setup_run_process_science_field(
        cluster_config=args.cluster_config,
        flint_ms_directory=args.flint_ms_directory,
        pol_field_options=field_options,
        ms_is_flint=args.ms_is_flint,
    )


if __name__ == "__main__":
    cli()
