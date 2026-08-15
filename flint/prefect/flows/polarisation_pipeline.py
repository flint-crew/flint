from __future__ import annotations

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
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    linmos_channel_groups_to_cubes,
    task_convolve_images,
    task_create_name_from_common_fields,
    task_get_channel_images_from_paths,
    task_get_common_beam_from_image_set,
    task_merge_image_sets,
    task_preprocess_askap_ms,
    task_remove_files_folders,
    task_split_and_get_image_set,
    task_transpose_and_sort_channel_images,
    task_wsclean_imager,
)
from flint.prefect.common.rmsynth import task_rmsynth_and_write_products
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
