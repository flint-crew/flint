"""This is a workflow to subtract a continuum model and image the channel-wise data

Unlike the continuum imaging and self-calibnration pipeline this flow currently
expects that all measurement sets are in the flint format, which means other than
the naming scheme that they have been been preprocessed to place them in the IAU
frame and have had their fields table updated. That is to say that they have
already been preprocessed and fixed.
"""

from __future__ import annotations

from pathlib import Path
from time import sleep

import numpy as np
from configargparse import ArgumentParser
from fitscube.combine_fits import combine_fits
from prefect import flow, task, unmapped

from flint.coadd.linmos import LinmosResult
from flint.configuration import get_options_from_strategy, load_and_copy_strategy
from flint.exceptions import FrequencyMismatchError
from flint.logging import logger
from flint.ms import (
    MS,
    consistent_ms_frequencies,
    find_mss,
    get_freqs_from_ms,
)
from flint.naming import get_sbid_from_path
from flint.options import (
    AddModelSubtractFieldOptions,
    SubtractFieldOptions,
    add_options_to_parser,
    create_options_from_parser,
)
from flint.predict.crystalball import CrystalBallOptions
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    convolve_then_linmos,
    task_get_common_beam_from_results,
    task_wsclean_imager,
)
from flint.prefect.common.ms import task_subtract_model_from_ms
from flint.prefect.common.predict import task_addmodel_to_ms, task_crystalball_to_ms


def _check_and_verify_options(
    subtract_field_options: SubtractFieldOptions | None = None,
    addmodel_subtract_field_options: AddModelSubtractFieldOptions | None = None,
    crystalball_subtract_field_options: CrystalBallOptions | None = None,
) -> None:
    """Verify that the options supplied to run the subtract field options make sense"""
    if subtract_field_options:
        assert (
            subtract_field_options.wsclean_container.exists()
            and subtract_field_options.wsclean_container.is_file()
        ), (
            f"{subtract_field_options.wsclean_container=} does not exist or is not a file"
        )
        assert (
            subtract_field_options.yandasoft_container.exists()
            and subtract_field_options.yandasoft_container.is_file()
        ), (
            f"{subtract_field_options.yandasoft_container=} does not exist or is not a file"
        )

    if subtract_field_options and subtract_field_options.use_addmodel:
        assert addmodel_subtract_field_options, (
            "Addmodel usage invoked by no AddModelOptions supplied"
        )
        assert addmodel_subtract_field_options.calibrate_container is not None, (
            "Calibrate container path is needede for addmodel"
        )
        assert (
            addmodel_subtract_field_options.calibrate_container.exists()
            and addmodel_subtract_field_options.calibrate_container.is_file()
        ), (
            f"Calibrate container {addmodel_subtract_field_options.calibrate_container} is not a file"
        )
        assert addmodel_subtract_field_options.addmodel_cluster_config is not None, (
            f"{addmodel_subtract_field_options.addmodel_cluster_config=}, which should not happen"
        )

    if subtract_field_options:
        assert (
            sum(
                [
                    subtract_field_options.use_addmodel,
                    subtract_field_options.use_crystalball,
                ]
            )
            <= 1
        ), "Both addmodel and crystallball selected. This surely can not be right. "


def find_mss_to_image(
    mss_parent_path: Path,
    expected_ms_count: int | None = None,
    data_column: str = "CORRECTED_DATA",
    model_column: str = "MODEL_DATA",
) -> tuple[MS, ...]:
    """Search for MSs to image. See ``flint.ms.find_mss`` for further details.

    Args:
        mss_parent_path (Path): Path to search for MSs in
        expected_ms_count (Optional[int], optional): Expected number of measurement sets to find. Defaults to None.
        data_column (str, optional): The nominated data column that should eb set. Defaults to "CORRECTED_DATA".
        model_column (str, optional): The nominated model data column that should be set. Defaults to "MODEL_DATA".

    Returns:
        Tuple[MS, ...]: Collect of MSs
    """
    science_mss = find_mss(
        mss_parent_path=mss_parent_path,
        expected_ms_count=expected_ms_count,
        data_column=data_column,
        model_column=model_column,
    )
    logger.info(f"Found {science_mss=}")
    return science_mss


def find_and_setup_mss(
    science_path_or_mss: Path | tuple[MS, ...],
    expected_ms_count: int,
    data_column: str,
) -> tuple[MS, ...]:
    """Search for MSs in a directory and, if necessary, perform checks around
    their consistency. If the input data appear to be collection of MSs already
    assume they have already been set and checked for consistency.

    Args:
        science_path_or_mss (Union[Path, List[MS, ...]]): Path to search or existing MSs
        expected_ms_count (int): Expected number of MSs to find
        data_column (str): The data column to nominate if creating MSs after searching

    Raises:
        FrequencyMismatchError: Raised when frequency information is not consistent

    Returns:
        Tuple[MS, ...]: Collection of MSs
    """

    if isinstance(science_path_or_mss, (list, tuple)):
        logger.info("Already loaded MSs")
        return tuple(sms for sms in science_path_or_mss)

    # Find the MSs
    # - optionally untar?
    science_mss = find_mss_to_image(
        mss_parent_path=science_path_or_mss,
        expected_ms_count=expected_ms_count,
        data_column=data_column,
    )

    # 2 - ensure matchfing frequencies over channels
    consistent_frequencies_across_mss = consistent_ms_frequencies(mss=science_mss)
    if not consistent_frequencies_across_mss:
        logger.critical("Mismatch in frequencies among provided MSs")
        raise FrequencyMismatchError("There is a mismatch in frequencies")

    return science_mss


@task
def task_combine_all_linmos_images(
    linmos_commands: list[LinmosResult],
    remove_original_images: bool = False,
    combine_weights: bool = False,
) -> Path:
    output_cube_path = Path("test.fits")

    if combine_weights:
        logger.info("Combining weight fits files")
        images_to_combine = [
            linmos_command.weight_fits for linmos_command in linmos_commands
        ]
        output_suffix = "weight"
    else:
        logger.info("Combining image fits files")
        images_to_combine = [
            linmos_command.image_fits for linmos_command in linmos_commands
        ]
        output_suffix = "linmos"

    logger.info(f"Combining {len(images_to_combine)} FITS files together")

    from flint.naming import create_image_cube_name, create_name_from_common_fields

    assert len(images_to_combine) > 0, "No images to combine"

    base_cube_path = create_name_from_common_fields(in_paths=tuple(images_to_combine))
    output_cube_path = create_image_cube_name(
        image_prefix=base_cube_path, mode="contsub", suffix=output_suffix
    )

    _ = combine_fits(
        file_list=images_to_combine,
        out_cube=output_cube_path,
        max_workers=4,
    )

    if remove_original_images:
        logger.info(f"Removing original {len(images_to_combine)} images")
        for image in images_to_combine:
            logger.info(f"Removing {image=}")
            assert isinstance(image, Path) and image.exists(), (
                f"{image=} does not exist, but it should"
            )
            image.unlink()
    return Path(output_cube_path)


@flow
def flow_addmodel_to_mss(
    science_path_or_mss: Path | tuple[MS, ...],
    addmodel_subtract_field_options: AddModelSubtractFieldOptions,
    expected_ms: int,
    data_column: str,
) -> tuple[MS, ...]:
    """Separate flow to perform the potentially expensive model prediction
    into MSs"""
    _check_and_verify_options(
        addmodel_subtract_field_options=addmodel_subtract_field_options
    )

    # Get the MSs that will have their model added to
    science_mss = find_and_setup_mss(
        science_path_or_mss=science_path_or_mss,
        expected_ms_count=expected_ms,
        data_column=data_column,
    )
    science_mss = task_addmodel_to_ms.map(
        ms=science_mss,
        addmodel_subtract_options=unmapped(addmodel_subtract_field_options),
    )

    return science_mss


@flow
def flow_subtract_cube(
    science_path: Path,
    subtract_field_options: SubtractFieldOptions,
    addmodel_subtract_field_options: AddModelSubtractFieldOptions,
    crystalball_subtract_field_options: CrystalBallOptions,
) -> None:
    strategy = load_and_copy_strategy(
        output_split_science_path=science_path,
        imaging_strategy=subtract_field_options.imaging_strategy,
    )
    _check_and_verify_options(
        subtract_field_options=subtract_field_options,
        addmodel_subtract_field_options=addmodel_subtract_field_options,
        crystalball_subtract_field_options=crystalball_subtract_field_options,
    )

    # Find the MSs
    # - optionally untar?
    science_mss = find_and_setup_mss(
        science_path_or_mss=science_path,
        expected_ms_count=subtract_field_options.expected_ms,
        data_column=subtract_field_options.data_column,
    )

    # 2.5 - Continuum subtract if requested

    freqs_mhz = get_freqs_from_ms(ms=science_mss[0]) / 1e6
    logger.info(
        f"Considering {len(freqs_mhz)} frequencies from {len(science_mss)} channels, minimum {np.min(freqs_mhz)}-{np.max(freqs_mhz)}"
    )
    if len(freqs_mhz) > 20 and subtract_field_options.stagger_delay_seconds is None:
        logger.critical(
            f"{len(freqs_mhz)} channels and no stagger delay set! Consider setting a stagger delay"
        )

    if subtract_field_options.use_addmodel:
        assert addmodel_subtract_field_options.addmodel_cluster_config is not None, (
            f"{addmodel_subtract_field_options.addmodel_cluster_config=}, which should not happen"
        )
        addmodel_dask_runner = get_dask_runner(
            cluster=addmodel_subtract_field_options.addmodel_cluster_config
        )
        science_mss = flow_addmodel_to_mss.with_options(
            task_runner=addmodel_dask_runner, name="Predict -- Addmodel"
        )(
            science_path_or_mss=science_mss,
            addmodel_subtract_field_options=addmodel_subtract_field_options,
            expected_ms=subtract_field_options.expected_ms,
            data_column=subtract_field_options.data_column,
        )

    if subtract_field_options.use_crystalball:
        logger.info("Attempting to peer into the crystalball, me'hearty")
        science_mss = task_crystalball_to_ms.map(
            ms=science_mss,
            crystalball_options=unmapped(crystalball_subtract_field_options),
        )

    if subtract_field_options.attempt_subtract:
        science_mss = task_subtract_model_from_ms.map(
            ms=science_mss,
            data_column=subtract_field_options.subtract_data_column,
            update_tracked_column=True,
        )

    channel_parset_list = []
    for channel, freq_mhz in enumerate(freqs_mhz):
        logger.info(f"Imaging {channel=} {freq_mhz=}")
        channel_range = (channel, channel + 1)
        channel_wsclean_cmds = task_wsclean_imager.with_options(retries=2).map(
            in_ms=science_mss,
            wsclean_container=subtract_field_options.wsclean_container,
            channel_range=unmapped(channel_range),
            update_wsclean_options=unmapped(
                get_options_from_strategy(
                    strategy=strategy,
                    mode="wsclean",
                    operation="subtractcube",
                )
            ),
        )
        channel_beam_shape = task_get_common_beam_from_results.submit(
            wsclean_results=channel_wsclean_cmds,
            cutoff=subtract_field_options.beam_cutoff,
            filter_str="image.",
        )
        channel_parset = convolve_then_linmos(
            wsclean_results=channel_wsclean_cmds,
            beam_shape=channel_beam_shape,
            linmos_suffix_str=None,
            field_options=subtract_field_options,
            convol_mode="image",
            convol_filter="image.",
            convol_suffix_str="optimal.conv",
            trim_linmos_fits=False,  # This is necessary to ensure all images have same pixel-coordinates
            remove_original_images=True,
            cleanup_linmos=True,
        )
        channel_parset_list.append(channel_parset)

        if subtract_field_options.stagger_delay_seconds:
            sleep(subtract_field_options.stagger_delay_seconds)

    # 4 - cube concatenated each linmos field together to single file
    task_combine_all_linmos_images.submit(
        linmos_commands=channel_parset_list, remove_original_images=True
    )
    task_combine_all_linmos_images.submit(
        linmos_commands=channel_parset_list,
        remove_original_images=True,
        combine_weights=True,
    )

    return


def setup_run_subtract_flow(
    science_path: Path,
    subtract_field_options: SubtractFieldOptions,
    addmodel_subtract_field_options: AddModelSubtractFieldOptions,
    crystalball_subtract_field_options: CrystalBallOptions,
    cluster_config: Path,
) -> None:
    logger.info(f"Processing {science_path=}")
    science_sbid = get_sbid_from_path(path=science_path)

    dask_runner = get_dask_runner(cluster=cluster_config)

    flow_subtract_cube.with_options(
        task_runner=dask_runner, name=f"Subtract Cube Pipeline -- {science_sbid}"
    )(
        science_path=science_path,
        subtract_field_options=subtract_field_options,
        addmodel_subtract_field_options=addmodel_subtract_field_options,
        crystalball_subtract_field_options=crystalball_subtract_field_options,
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )
    parser.add_argument(
        "science_path",
        type=Path,
        help="Path to the directory containing the beam-wise measurement sets",
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(parser=parser, options_class=SubtractFieldOptions)
    parser = add_options_to_parser(
        parser=parser, options_class=AddModelSubtractFieldOptions
    )
    parser = add_options_to_parser(parser=parser, options_class=CrystalBallOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    subtract_field_options = create_options_from_parser(
        parser_namespace=args, options_class=SubtractFieldOptions
    )
    addmodel_subtract_field_options = create_options_from_parser(
        parser_namespace=args, options_class=AddModelSubtractFieldOptions
    )
    crystalball_options = create_options_from_parser(
        parser_namespace=args, options_class=CrystalBallOptions
    )

    if addmodel_subtract_field_options.addmodel_cluster_config is None:
        addmodel_subtract_field_options.with_options(
            addmodel_cluster_config=args.cluster_config
        )

    setup_run_subtract_flow(
        science_path=args.science_path,
        subtract_field_options=subtract_field_options,
        addmodel_subtract_field_options=addmodel_subtract_field_options,
        crystalball_subtract_field_options=crystalball_options,
        cluster_config=args.cluster_config,
    )


if __name__ == "__main__":
    cli()
