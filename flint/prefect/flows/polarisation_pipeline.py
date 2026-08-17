from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from prefect import flow, tags
from prefect.futures import PrefectFuture

from flint.coadd.linmos import LinmosOptions
from flint.configuration import (
    POLARISATION_MAPPING,
    get_options_from_strategy,
    load_and_copy_strategy,
)
from flint.exceptions import MSError
from flint.imager.channel_division import ChannelDivision, apply_cube_division
from flint.imager.wsclean import (
    ImageSet,
    WSCleanResult,
)
from flint.logging import logger
from flint.ms import MSsByBeam, find_mss
from flint.naming import (
    CASDANameComponents,
    ProcessedNameComponents,
    add_timestamp_to_path,
    extract_components_from_name,
    get_sbid_from_path,
)
from flint.options import (
    FitsCubeOptions,
    PolFieldOptions,
    RMCleanOptions,
    RMSynthOptions,
    SpiceOptions,
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    linmos_channel_groups_to_cubes,
    task_convolve_images,
    task_create_name_from_common_fields,
    task_get_channel_images_from_paths,
    task_get_common_beam_from_image_set,
    task_get_mfs_image_from_paths,
    task_linmos_images,
    task_merge_image_sets,
    task_preprocess_askap_ms,
    task_remove_files_folders,
    task_run_bane_and_aegean,
    task_split_and_get_image_set,
    task_transpose_and_sort_channel_images,
    task_wsclean_imager,
)
from flint.prefect.common.rmsynth import task_rmsynth_and_write_products
from flint.prefect.common.spice import (
    task_compress_cube,
    task_get_spice_boxes,
    task_spice_fits,
)
from flint.prefect.common.utils import (
    task_create_field_summary,
    task_getattr,
)

# Marks images/cubes produced by this pipeline so they don't clash with the
# continuum self-cal flow's own Stokes I/V products for the same MS/beam.
POL_NAME_SUFFIX = "pol"


@flow(name="Flint Polarisation Pipeline")
def process_science_fields_pol(
    flint_ms_directory: Path,
    pol_field_options: PolFieldOptions,
    cube_division: ChannelDivision | None = None,
    mss_by_beam: MSsByBeam | None = None,
) -> list[PrefectFuture[Any]]:
    # returned futures are resolved by prefect to fail the flow on task failure
    strategy = load_and_copy_strategy(
        output_split_science_path=flint_ms_directory,
        imaging_strategy=pol_field_options.imaging_strategy,
    )

    logger.info(f"{pol_field_options=}")

    if strategy is None:
        logger.info("No strategy provided. Returning.")
        return []

    if mss_by_beam is not None:
        # Already Flint-processed, self-calibrated MSs handed down by the calling
        # flow (e.g. the RACS-All continuum flow). No need to rediscover them on
        # disk or check whether they are CASDA-provided.
        logger.info("Using the measurement sets supplied by the calling flow")
        resolved_mss_by_beam: MSsByBeam = mss_by_beam
    else:
        # Get some placeholder names
        science_mss = list(
            find_mss(
                mss_parent_path=flint_ms_directory,
                expected_ms_count=pol_field_options.expected_ms,
                data_column=strategy["defaults"].get("data_column", "DATA"),
            )
        )
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
            msg = "No valid MeasurementSets found! Data must be calibrated first."
            raise MSError(msg)

        if any(from_flint_list) and any(from_casda_list):
            msg = "Cannot mix Flint-processed and CASDA-provided MeasurementSets!"
            raise MSError(msg)

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

        # Each beam is a single MS when discovered this way (one SBID/band per directory)
        resolved_mss_by_beam = tuple((ms,) for ms in science_mss)

    science_mss = [ms for beam_mss in resolved_mss_by_beam for ms in beam_mss]

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
        return [field_summary]

    polarisations: dict[str, str] = strategy.get("polarisation", {"total": {}})

    image_sets_dict: dict[str, PrefectFuture[ImageSet]] = {}
    image_sets_list: list[PrefectFuture[ImageSet]] = []
    for polarisation in polarisations.keys():
        _image_sets = []
        with tags(f"polarisation-{polarisation}"):
            for beam_mss in resolved_mss_by_beam:
                update_wsclean_options = get_options_from_strategy(
                    strategy=strategy,
                    operation="polarisation",
                    mode="wsclean",
                    polarisation=polarisation,
                )
                update_wsclean_options["flint_name_suffix"] = POL_NAME_SUFFIX
                if cube_division is not None:
                    update_wsclean_options = apply_cube_division(
                        update_wsclean_options=update_wsclean_options,
                        cube_division=cube_division,
                    )
                wsclean_result: PrefectFuture[WSCleanResult] = (
                    task_wsclean_imager.submit(
                        in_ms=beam_mss,
                        wsclean_container=pol_field_options.wsclean_container,
                        make_cube_from_subbands=False,  # We will do this later
                        update_wsclean_options=update_wsclean_options,
                    )
                )
                _image_set: PrefectFuture[ImageSet] = task_getattr.submit(
                    wsclean_result, "image_set"
                )
                _image_sets.append(_image_set)
                image_sets_list.append(_image_set)
        image_sets_dict[polarisation] = _image_sets

    merged_image_set = task_merge_image_sets.submit(image_sets=image_sets_list)

    common_beam_shape = task_get_common_beam_from_image_set.submit(
        image_set=merged_image_set,
        cutoff=pol_field_options.beam_cutoff,
        fixed_beam_shape=pol_field_options.fixed_beam_shape,
    )

    # Convolve every beam's sub-band images to the common beam, keeping the
    # per-channel images (rather than cubing per beam) so we can co-add across
    # beams one channel at a time.
    stokes_beam_channel_images: dict[str, list[PrefectFuture[list[Path]]]] = {}
    # Per-beam Stokes I MFS images, used by run_spice both as the field WCS
    # reference for box-building and (absent a user spice_catalogue) as the
    # image source-found against. wsclean already produces these; only the
    # collection here is new.
    beam_mfs_images: list[PrefectFuture[Path]] = []
    for polarisation, image_set_list in image_sets_dict.items():
        with tags(f"polarisation-{polarisation}"):
            # Get the individual Stokes parameters in case of joint imaging
            if polarisation not in POLARISATION_MAPPING.keys():
                raise ValueError(f"Unknown polarisation {polarisation}")
            stokes_list = list(POLARISATION_MAPPING[polarisation])
            for stokes in stokes_list:
                with tags(f"stokes-{stokes}"):
                    beam_channel_images: list[PrefectFuture[list[Path]]] = []
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
                        if stokes == "i" and pol_field_options.run_spice:
                            beam_mfs_images.append(
                                task_get_mfs_image_from_paths.submit(
                                    paths=convolved_image_list
                                )
                            )
                        channel_image_list = task_get_channel_images_from_paths.submit(
                            paths=convolved_image_list
                        )
                        beam_channel_images.append(channel_image_list)
                    stokes_beam_channel_images[stokes] = beam_channel_images

    # Regroup each Stokes' per-beam channel images into per-channel beam groups
    # so linmos can run one channel at a time in parallel. Resolving here blocks
    # until the convolutions above have completed.
    stokes_channel_groups: dict[str, list[list[Path]]] = {
        stokes: task_transpose_and_sort_channel_images.submit(
            beam_channel_images=beam_channel_images
        ).result()
        for stokes, beam_channel_images in stokes_beam_channel_images.items()
    }

    # Stokes I beam images (per channel) are needed to correct widefield leakage
    # in the Stokes Q/U mosaics. If Stokes I was not imaged we cannot do this.
    i_channel_groups = stokes_channel_groups.get("i")
    force_remove_leakage: bool | None = None if i_channel_groups else False

    assert pol_field_options.yandasoft_container is not None

    fitscube_options = FitsCubeOptions().with_options(
        **get_options_from_strategy(
            strategy=strategy,
            operation="polarisation",
            mode="fitscube",
        )
    )

    island_boxes: PrefectFuture[list] | None = None
    if pol_field_options.run_spice:
        # Required even with a user-supplied spice_catalogue: the MFS mosaic
        # built below is the earliest field-level WCS reference available,
        # needed to turn that catalogue's RA/Dec into pixel boxes.
        assert i_channel_groups is not None, (
            "run_spice requires the 'total' polarisation (Stokes I) to have been imaged"
        )
        assert (
            pol_field_options.spice_catalogue is not None
            or pol_field_options.aegean_container is not None
        ), "run_spice without a spice_catalogue requires an aegean_container"

        mfs_linmos_result = task_linmos_images.submit(
            image_list=beam_mfs_images,
            container=pol_field_options.yandasoft_container,
            linmos_options=LinmosOptions(
                holofile=pol_field_options.holofile,
                cutoff=pol_field_options.pb_cutoff,
                cleanup=True,
                # Must match the untrimmed shape of the per-channel/cube images
                # spice_fits later compares this reference's shape against.
                trim_linmos_fits=False,
            ),
            field_summary=field_summary,
            holofile=pol_field_options.holofile,
        )
        mfs_image_path = task_getattr.submit(mfs_linmos_result, "image_fits")

        is_user_catalogue = pol_field_options.spice_catalogue is not None
        if is_user_catalogue:
            catalogue_path = pol_field_options.spice_catalogue
        else:
            aegean_outputs = task_run_bane_and_aegean.submit(
                image=mfs_linmos_result,
                aegean_container=pol_field_options.aegean_container,
                update_bane_options=get_options_from_strategy(
                    strategy=strategy, operation="polarisation", mode="bane"
                ),
                update_aegean_options=get_options_from_strategy(
                    strategy=strategy, operation="polarisation", mode="aegean"
                ),
            )
            catalogue_path = task_getattr.submit(aegean_outputs, "comp")

        spice_options = SpiceOptions(
            **get_options_from_strategy(
                strategy=strategy, operation="polarisation", mode="spice"
            )
        )
        island_boxes = task_get_spice_boxes.submit(
            reference_image=mfs_image_path,
            catalogue=catalogue_path,
            spice_options=spice_options,
            beam_shape=common_beam_shape,
            is_user_catalogue=is_user_catalogue,
        )

    # Whether anything downstream needs the full-size (unspiced) cube: either
    # spice is off entirely, or rm-synth is on and reading the full cube.
    need_full_cube = not pol_field_options.run_spice or (
        pol_field_options.run_rmsynth and not pol_field_options.rmsynth_on_spiced_cubes
    )

    cube_results: list[PrefectFuture[Path]] = []
    stokes_image_cubes: dict[str, PrefectFuture[Path]] = {}
    all_input_images: list[Path] = []
    for stokes, channel_groups in stokes_channel_groups.items():
        with tags(f"stokes-{stokes}"):
            all_input_images.extend(
                [image for beam_images in channel_groups for image in beam_images]
            )
            stokes_cubes = linmos_channel_groups_to_cubes(
                channel_groups=channel_groups,
                container=pol_field_options.yandasoft_container,
                linmos_options=LinmosOptions(
                    holofile=pol_field_options.holofile,
                    cutoff=pol_field_options.pb_cutoff,
                    force_remove_leakage=force_remove_leakage,
                    cleanup=True,
                ),
                stokesi_channel_groups=i_channel_groups,
                field_summary=field_summary,
                fitscube_options=fitscube_options,
                suffix_str=POL_NAME_SUFFIX,
                plane_post_process=partial(task_spice_fits.submit, boxes=island_boxes)
                if pol_field_options.run_spice and not need_full_cube
                else None,
            )
            stokes_image_cubes[stokes] = stokes_cubes[0]
            cube_results.extend(stokes_cubes)

    # Remove the convolved per-beam channel images now that every cube is built.
    # Stokes I images are kept until here as they feed the Q/U leakage correction.
    remove_result = task_remove_files_folders.submit(
        *all_input_images, wait_for=cube_results
    )

    rmsynth_result: PrefectFuture[list[Path]] | None = None
    if pol_field_options.run_rmsynth:
        assert "q" in stokes_image_cubes and "u" in stokes_image_cubes, (
            "run_rmsynth requires the 'linear' polarisation (Stokes Q/U) to have been imaged"
        )
        # Mirrors the i_channel_groups leakage-correction gating above: use
        # Stokes I if it was imaged, otherwise skip the fractional-pol correction.
        stokes_i_cube = stokes_image_cubes.get("i")
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
        output_prefix = task_create_name_from_common_fields.submit(
            in_paths=(stokes_image_cubes["q"], stokes_image_cubes["u"])
        )
        rmsynth_result = task_rmsynth_and_write_products.submit(
            stokes_q_cube=stokes_image_cubes["q"],
            stokes_u_cube=stokes_image_cubes["u"],
            stokes_i_cube=stokes_i_cube,
            rmsynth_options=rmsynth_options,
            rmclean_options=rmclean_options,
            cube_products=pol_field_options.rmsynth_cube_products,
            moment_products=pol_field_options.rmsynth_moment_products,
            output_prefix=output_prefix,
        )

    if pol_field_options.run_spice:
        if need_full_cube:
            # Case 3: rm-synth (always on here, see need_full_cube above) has
            # already read these full cubes -- now replace them in place.
            spiced_cubes = [
                task_spice_fits.submit(
                    fits_path=cube, boxes=island_boxes, wait_for=[rmsynth_result]
                )
                for cube in cube_results
            ]
            compress_wait_for = None
        else:
            # Case 2: cube_results are already the spiced cubes (spice ran as
            # plane_post_process before cubing). If rm-synth read them
            # (rmsynth_on_spiced_cubes), keep them uncompressed until it's done.
            spiced_cubes = cube_results
            compress_wait_for = [rmsynth_result] if rmsynth_result else None

        cube_results = [
            task_compress_cube.submit(
                out_cube=cube,
                method=spice_options.compress_method,
                max_workers=spice_options.compress_max_workers,
                wait_for=compress_wait_for,
            )
            for cube in spiced_cubes
        ]

    return [*cube_results, remove_result, *([rmsynth_result] if rmsynth_result else [])]


def setup_run_process_science_field(
    cluster_config: str | Path,
    flint_ms_directory: Path,
    pol_field_options: PolFieldOptions,
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
    )


if __name__ == "__main__":
    cli()
