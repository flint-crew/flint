from __future__ import annotations

from pathlib import Path
from typing import Any

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
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
from flint.imager.channel_division import (
    ChannelDivision,
    apply_cube_division,
    channel_division_for_beams,
)
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
    dump_field_options_to_yaml,
)
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    convolve_channel_groups_to_natural_resolution,
    convolve_mfs_beam_images_to_common_resolution,
    linmos_channel_groups_to_cubes,
    task_get_channel_images_from_paths,
    task_get_mfs_image_from_paths,
    task_linmos_images,
    task_preprocess_askap_ms,
    task_remove_files_folders,
    task_split_and_get_image_set,
    task_transpose_and_sort_channel_images,
    task_wsclean_imager,
)
from flint.prefect.common.utils import (
    task_create_field_summary,
    task_getattr,
)

# Marks images/cubes produced by this pipeline so they don't clash with the
# continuum self-cal flow's own Stokes I/V products for the same MS/beam.
POL_NAME_SUFFIX = "pol"


class PolPipelineResult(BaseOptions):
    """Return value of ``process_science_fields_pol``, handed in-memory to
    the rm-synth/clean and spice-compression stages of the ``racs-all``
    flow-of-flows."""

    stokes_cubes: dict[str, Path]
    """The full, unspiced Stokes cube written for each imaged polarisation (e.g. 'i', 'q', 'u', 'v')"""
    weight_cubes: dict[str, Path]
    """The full Stokes weights written for each imaged polarisation (e.g. 'i', 'q', 'u', 'v')"""
    mfs_products: dict[str, dict[str, Path]]
    """MFS image/model/residual products co-added per Stokes parameter, keyed by Stokes ('i', 'q', 'u', 'v') then product type ('image', 'model', 'residual'). Only populated for Stokes imaged under a polarisation with ``WSCleanOptions.flint_save_mfs_products`` set"""
    terminal_futures: list[PrefectFuture[Any]]
    """Every future the polarisation stage produced, propagated so Prefect still detects any of their failures"""


def _no_products(
    terminal_futures: list[PrefectFuture[Any]] | None = None,
) -> PolPipelineResult:
    """The result of a polarisation stage that imaged nothing.

    Built here rather than at each early return so that a field added to
    ``PolPipelineResult`` cannot turn one of them into a ValidationError: the
    downstream rm-synth stage reads ``weight_cubes`` off this, and an early
    return that forgot it failed only once the flow was already running.

    Args:
        terminal_futures (list[PrefectFuture[Any]] | None, optional): Futures produced before the stage gave up, propagated so Prefect still detects their failures. Defaults to None.

    Returns:
        PolPipelineResult: An empty result carrying only ``terminal_futures``
    """
    return PolPipelineResult(
        stokes_cubes={},
        weight_cubes={},
        mfs_products={},
        terminal_futures=terminal_futures or [],
    )


@flow(name="Flint Polarisation Pipeline")
def process_science_fields_pol(
    flint_ms_directory: Path,
    pol_field_options: PolFieldOptions,
    mss_by_beam: MSsByBeam | None = None,
    compress_cubes: bool | None = None,
) -> PolPipelineResult:
    """Image a field in polarisation and co-add the per-beam products.

    Args:
        flint_ms_directory (Path): Directory holding the measurement sets to image
        pol_field_options (PolFieldOptions): Options controlling the polarisation imaging
        mss_by_beam (MSsByBeam | None, optional): Already Flint-processed measurement sets handed down by a calling flow, rather than rediscovered on disk. Defaults to None.
        compress_cubes (bool | None, optional): Overrides the strategy ``fitscube`` compress setting. A calling flow that reads these cubes afterwards (rm-synth, spice) has to set it False, since astropy cannot memmap a gzip file and a chunked read would inflate the whole cube into memory. None honours the strategy. Defaults to None.

    Returns:
        PolPipelineResult: Stokes cubes, co-added MFS products, and the futures to wait on
    """
    strategy = load_and_copy_strategy(
        output_split_science_path=flint_ms_directory,
        imaging_strategy=pol_field_options.imaging_strategy,
    )

    logger.info(f"{pol_field_options=}")

    if strategy is None:
        logger.info("No strategy provided. Returning.")
        return _no_products()

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
        return _no_products(terminal_futures=[field_summary])

    polarisations: dict[str, str] = strategy.get("polarisation", {"total": {}})

    # Solved once for all beams, and before any imaging
    cube_division: ChannelDivision | None = None
    if pol_field_options.pol_cube_channel_width:
        cube_division = channel_division_for_beams(
            mss_by_beam=[
                [
                    ms.result() if isinstance(ms, PrefectFuture) else ms
                    for ms in beam_mss
                ]
                for beam_mss in resolved_mss_by_beam
            ],
            target_width=pol_field_options.pol_cube_channel_width,
        )

    image_sets_dict: dict[str, PrefectFuture[ImageSet]] = {}
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
                save_mfs_products = update_wsclean_options.get(
                    "flint_save_mfs_products", False
                )
                wsclean_result: PrefectFuture[WSCleanResult] = (
                    task_wsclean_imager.submit(
                        in_ms=beam_mss,
                        wsclean_container=pol_field_options.wsclean_container,
                        make_cube_from_subbands=False,  # We will do this later
                        update_wsclean_options=update_wsclean_options,
                        extra_output_types=("model",) if save_mfs_products else None,
                    )
                )
                _image_set: PrefectFuture[ImageSet] = task_getattr.submit(
                    wsclean_result, "image_set"
                )
                _image_sets.append(_image_set)
        image_sets_dict[polarisation] = _image_sets

    # Split each beam's images out per Stokes, leaving them unconvolved. The
    # cubes are brought to a 'natural' resolution, one common beam per channel,
    # and a channel's beam is solved over every beam image of every Stokes at
    # that channel, so nothing can be convolved until all of them are in hand.
    # The RM-synthesis stage brings its own inputs to a single 'total' beam; that
    # is a resolution to synthesise at, not one to archive the cubes at.
    stokes_beam_channel_images: dict[str, list[PrefectFuture[list[Path]]]] = {}
    # Per-beam MFS image/model/residual, collected per Stokes whenever that
    # Stokes' polarisation strategy sets flint_save_mfs_products. Co-added
    # further down the same way as the science image/cube.
    mfs_beam_images: dict[str, dict[str, list[PrefectFuture[Path]]]] = {}
    for polarisation, image_set_list in image_sets_dict.items():
        with tags(f"polarisation-{polarisation}"):
            # Get the individual Stokes parameters in case of joint imaging
            if polarisation not in POLARISATION_MAPPING.keys():
                raise ValueError(f"Unknown polarisation {polarisation}")
            stokes_list = list(POLARISATION_MAPPING[polarisation])

            save_mfs_products = get_options_from_strategy(
                strategy=strategy,
                operation="polarisation",
                mode="wsclean",
                polarisation=polarisation,
            ).get("flint_save_mfs_products", False)
            product_types = (
                ("image", "model", "residual") if save_mfs_products else ("image",)
            )

            for stokes in stokes_list:
                with tags(f"stokes-{stokes}"):
                    beam_channel_images: list[PrefectFuture[list[Path]]] = []
                    for product_type in product_types:
                        beam_mfs_images: list[PrefectFuture[Path]] = []
                        for image_set in image_set_list:
                            stokes_image_list = task_split_and_get_image_set.submit(
                                image_set=image_set,
                                get=stokes,
                                by="pol",
                                mode=product_type,
                            )
                            if save_mfs_products:
                                beam_mfs_images.append(
                                    task_get_mfs_image_from_paths.submit(
                                        paths=stokes_image_list
                                    )
                                )
                            if product_type == "image":
                                beam_channel_images.append(
                                    task_get_channel_images_from_paths.submit(
                                        paths=stokes_image_list
                                    )
                                )
                        if save_mfs_products:
                            mfs_beam_images.setdefault(stokes, {})[product_type] = (
                                beam_mfs_images
                            )
                    stokes_beam_channel_images[stokes] = beam_channel_images

    # Regroup each Stokes' per-beam channel images into per-channel beam groups
    # so a beam can be solved for each channel, and so linmos can then run one
    # channel at a time in parallel. Resolving here blocks until the imaging
    # above has completed.
    stokes_channel_groups: dict[str, list[list[Path]]] = {
        stokes: task_transpose_and_sort_channel_images.submit(
            beam_channel_images=beam_channel_images
        ).result()
        for stokes, beam_channel_images in stokes_beam_channel_images.items()
    }
    stokes_channel_groups = convolve_channel_groups_to_natural_resolution(
        stokes_channel_groups=stokes_channel_groups,
        cutoff=pol_field_options.beam_cutoff,
        fixed_beam_shape=pol_field_options.fixed_beam_shape,
    )

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
    if compress_cubes is not None:
        fitscube_options = fitscube_options.with_options(compress=compress_cubes)

    cube_results: list[PrefectFuture[Path]] = []
    stokes_image_cubes: dict[str, PrefectFuture[Path]] = {}
    stokes_weight_cubes: dict[str, PrefectFuture[Path]] = {}
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
            stokes_image_cubes[stokes], stokes_weight_cubes[stokes] = stokes_cubes
            cube_results.extend(stokes_cubes)

    # Remove the convolved per-beam channel images now that every cube is built.
    # Stokes I images are kept until here as they feed the Q/U leakage correction.
    remove_result = task_remove_files_folders.submit(
        *all_input_images, wait_for=cube_results
    )

    # An MFS product has no frequency axis for a natural beam to vary over, so
    # the MFS images get a single common beam of their own rather than the one
    # the coarsest channel of the cube needed. Resolving the futures here blocks
    # until the imaging has completed, which the beam solve needs regardless.
    mfs_beam_paths: dict[str, dict[str, list[Path]]] = {
        stokes: {
            product_type: [future.result() for future in beam_images]
            for product_type, beam_images in product_type_images.items()
        }
        for stokes, product_type_images in mfs_beam_images.items()
    }
    mfs_beam_paths = convolve_mfs_beam_images_to_common_resolution(
        mfs_beam_images=mfs_beam_paths,
        cutoff=pol_field_options.beam_cutoff,
        fixed_beam_shape=pol_field_options.fixed_beam_shape,
    )

    # Co-add the MFS image/model/residual products collected above the same way
    # as the science cube: PB-correct via linmos, leakage-correct against the
    # matching Stokes I MFS product where available, then clean up the
    # per-beam convolved intermediates.
    mfs_products: dict[str, dict[str, PrefectFuture[Path]]] = {}
    all_mfs_input_images: list[Path] = []
    mfs_linmos_results: list[PrefectFuture[Path]] = []
    for stokes, product_type_images in mfs_beam_paths.items():
        with tags(f"stokes-{stokes}"):
            for product_type, beam_images in product_type_images.items():
                with tags(f"product-{product_type}"):
                    stokesi_images: list[Path] | None = None
                    if stokes != "i":
                        stokesi_images = mfs_beam_paths.get("i", {}).get(product_type)

                    mfs_linmos_result = task_linmos_images.submit(
                        image_list=beam_images,
                        container=pol_field_options.yandasoft_container,
                        linmos_options=LinmosOptions(
                            holofile=pol_field_options.holofile,
                            cutoff=pol_field_options.pb_cutoff,
                            stokesi_images=stokesi_images,
                            force_remove_leakage=force_remove_leakage,
                            cleanup=True,
                        ),
                        field_summary=field_summary,
                        suffix_str=f"{POL_NAME_SUFFIX}.{product_type}",
                        holofile=pol_field_options.holofile,
                    )
                    mfs_image_path = task_getattr.submit(
                        mfs_linmos_result, "image_fits"
                    )
                    mfs_products.setdefault(stokes, {})[product_type] = mfs_image_path
                    all_mfs_input_images.extend(beam_images)
                    mfs_linmos_results.append(mfs_image_path)

    remove_mfs_result = (
        task_remove_files_folders.submit(
            *all_mfs_input_images, wait_for=mfs_linmos_results
        )
        if all_mfs_input_images
        else None
    )

    terminal_futures: list[PrefectFuture[Any]] = [*cube_results, remove_result]
    if remove_mfs_result is not None:
        terminal_futures.append(remove_mfs_result)

    return PolPipelineResult(
        stokes_cubes={
            stokes: future.result() for stokes, future in stokes_image_cubes.items()
        },
        weight_cubes={
            stokes: future.result() for stokes, future in stokes_weight_cubes.items()
        },
        mfs_products={
            stokes: {
                product_type: future.result()
                for product_type, future in product_type_futures.items()
            }
            for stokes, product_type_futures in mfs_products.items()
        },
        terminal_futures=terminal_futures,
    )


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
