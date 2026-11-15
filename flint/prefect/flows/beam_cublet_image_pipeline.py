"""Experimental flow that cleans one beam island by island.

Islands are found on a wide MFS image with few channels. Each is then imaged on
its own, in a small shifted image with many channels. Islands only meet in the
visibilities, so between rounds the components of all islands are predicted and
subtracted.
"""

from __future__ import annotations

import shutil
from argparse import ArgumentParser
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from eye_patch.naming import FITSMaskNames
from prefect import flow

from flint.configuration import (
    copy_and_timestamp_strategy_file,
    get_options_from_strategy,
    load_strategy_yaml,
)
from flint.cube import combine_images_to_cube
from flint.cublet.islands import (
    CubletRegion,
    find_cublet_regions,
    masked_peak,
    round_threshold,
    union_masks,
)
from flint.cublet.reordered import link_reordered_files
from flint.cublet.sourcelist import concatenate_source_lists
from flint.imager.wsclean import WSCleanResult, restore_source_list
from flint.logging import logger
from flint.ms import MS, get_freqs_from_ms
from flint.naming import add_timestamp_to_path, get_sbid_from_path
from flint.options import FitsCubeOptions, dump_field_options_to_yaml
from flint.predict.crystalball import CrystalBallOptions
from flint.prefect.caching import task
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.imaging import (
    task_create_image_mask_model,
    task_remove_files_folders,
    task_run_bane_and_aegean,
    task_wsclean_imager,
)
from flint.prefect.common.ms import task_subtract_model_from_ms
from flint.prefect.common.predict import task_crystalball_to_ms
from flint.prefect.helpers import watch_dask_cluster
from flint.utils import parse_environment_variables


class BeamCubletOptions(BaseOptions):
    """Options of the beam cublet flow"""

    ms_path: Path
    """The measurement set to image"""
    strategy: Path
    """Strategy file. The ``cubletimage`` operation is used for the MFS image and its mask, and ``cubletisland`` for the island images"""
    wsclean_container: Path
    """Path to the wsclean container"""
    cluster_config: Path
    """Path to the Dask configuration file"""
    aegean_container: Path | None = None
    """Path to the aegean container. Only needed if the mask uses RMS and background images"""
    column: str = "CORRECTED_DATA"
    """The column to image"""
    residual_column: str = "CUBLET_RESIDUAL"
    """The column the data less the components of all islands is written to. Every second subtraction writes to a column with an ``_ALT`` suffix, so that none subtracts from the column it reads"""
    cycles: int = 1
    """Number of times the whole beam is imaged to find islands. Later cycles image what the earlier ones left, and add the islands they find to those already known"""
    rounds: int = 3
    """Number of times every island is imaged in the first cycle, with the components subtracted in between"""
    later_rounds: int = 1
    """Number of rounds in each cycle after the first"""
    round_gain: float = 0.97
    """Fraction of the brightest island that each round cleans away. One less the far sidelobe level of the PSF is as deep as a round can usefully go"""
    threshold_floor: float = 0.0
    """Islands are never cleaned below this, in Jy, and one with nothing above it left inside its mask is not imaged. It only stops the cleaning, the masks decide where components go"""
    merge_pixels: int = 0
    """Islands closer than twice this many pixels are imaged as one"""
    pad_factor: float = 2.5
    """Size of an island image over the size of its island"""
    min_size: int = 128
    """Smallest island image, in pixels"""
    max_islands: int | None = None
    """Only image this many of the brightest islands"""
    cube_channels: int | None = None
    """Also make a restored cube of the whole beam with this many channels. Each channel is imaged without cleaning by a wsclean call of its own, and has all components added to it. By default no cube is made"""
    max_size: int | None = None
    """Ignore islands whose image would be larger than this, in pixels. They are not cleaned at all. By default none are ignored"""
    no_reuse_reordered: bool = False
    """Have every island reorder the visibilities for itself, rather than share those of the brightest island of the round"""
    keep_island_images: bool = False
    """Keep the FITS images of every island of every round. Otherwise they are deleted once the island is imaged, and only its components are kept"""
    watch_cluster: bool = False
    """Log what the dask cluster is doing once a minute, to see where a stall is"""
    work_dir: Path | None = None
    """Where island masks, component lists and reordered visibilities are kept. It has to be seen by every worker, and inside the wsclean container. Defaults to a ``cublet`` folder beside the measurement set"""


class IslandImage(BaseOptions):
    """What a round of imaging an island gives"""

    region: CubletRegion
    """The island, with its peak set to what is left of it"""
    tag: str
    """Name of the image, which is unique to the island and round"""
    source_list: Path | None = None
    """The clean components found"""


@task
def task_find_cublet_regions(
    wsclean_result: WSCleanResult,
    fits_mask: FITSMaskNames,
    output_dir: Path,
    beam_cublet_options: BeamCubletOptions,
) -> list[CubletRegion]:
    """Find the islands of the MFS image"""
    assert wsclean_result.image_set is not None, "No images from wsclean"
    # The MFS image is the last, as it is when the mask is made
    return find_cublet_regions(
        image_fits=list(wsclean_result.image_set.image)[-1],
        mask_names=fits_mask,
        output_dir=output_dir,
        merge_pixels=beam_cublet_options.merge_pixels,
        pad_factor=beam_cublet_options.pad_factor,
        min_size=beam_cublet_options.min_size,
        max_islands=beam_cublet_options.max_islands,
        max_size=beam_cublet_options.max_size,
    )


@task
def task_union_masks(
    mask_names: FITSMaskNames, earlier_mask_names: FITSMaskNames, output_dir: Path
) -> FITSMaskNames:
    """Add the islands of the earlier cycles to the mask of a cycle"""
    return union_masks(
        mask_names=mask_names,
        earlier_mask_names=earlier_mask_names,
        output_dir=output_dir,
    )


@task
def task_image_island(
    ms: MS,
    region: CubletRegion,
    wsclean_container: Path,
    update_wsclean_options: dict[str, Any],
    threshold: float,
    name_suffix: str,
    temp_dir: Path,
    save_reordered: bool = False,
    seed_dir: Path | None = None,
    keep_images: bool = False,
) -> IslandImage:
    """Image one island. With a ``seed_dir`` its reordered visibilities are used."""
    # The reordered files stay in ``temp_dir``, as the islands of a round share
    # them. A temp_dir in the strategy is where the island writes its images.
    hold_dir = update_wsclean_options.get(
        "flint_hold_dir"
    ) or update_wsclean_options.get("temp_dir")
    hold_dir = parse_environment_variables(variable=str(hold_dir)) if hold_dir else None
    hold_dir = Path(hold_dir) / name_suffix if hold_dir else temp_dir / "images"

    if seed_dir is not None:
        link_reordered_files(seed_dir=seed_dir, target_dir=temp_dir, ms_path=ms.path)

    # The rounds are the major cycles, so wsclean need not predict its own model
    island_options = {"mgain": 1.0, **update_wsclean_options}
    island_options.update(
        size=region.size,
        shift=region.shift,
        super_weight=region.super_weight,
        threshold=threshold,
        save_source_list=True,
        no_update_model_required=True,
        flint_name_suffix=name_suffix,
        temp_dir=temp_dir,
        # Otherwise the reordered files are moved out of the temp_dir with the images
        flint_hold_dir=hold_dir,
        # The channel images are then never written beside the measurement set
        flint_mfs_only=not keep_images,
        # A call that reuses reordered files deletes them unless told to save them
        save_reordered=save_reordered or seed_dir is not None,
        reuse_reordered=seed_dir is not None,
    )
    try:
        wsclean_result = task_wsclean_imager.fn(
            in_ms=ms,
            wsclean_container=wsclean_container,
            update_wsclean_options=island_options,
            fits_mask=region.fits_mask,
            make_cube_from_subbands=keep_images,
        )
    finally:
        # A failed call leaves its images behind, which on a ramdisk is memory
        shutil.rmtree(hold_dir, ignore_errors=True)
        if seed_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    image_set = wsclean_result.image_set
    assert image_set is not None, "No images from wsclean"

    # The MFS residual is the last, and the only one flint keeps
    peak = region.peak
    if image_set.residual and region.fits_mask:
        peak = masked_peak(
            image_fits=image_set.residual[-1],
            mask_fits=region.fits_mask.mask_fits or region.fits_mask.scale_mask_fits,
        )

    if not keep_images:
        # By name, as flint leaves images on disk that it does not gather, and
        # listing a directory that every island writes to is slow
        mfs = "MFS." if wsclean_result.options.channels_out > 1 else ""
        for image_type in ("image", "dirty", "psf", "model", "residual"):
            Path(f"{image_set.prefix}.{mfs}{image_type}.fits").unlink(missing_ok=True)

    return IslandImage(
        region=region.with_options(peak=peak),
        tag=name_suffix,
        source_list=image_set.source_list,
    )


@task
def task_concatenate_source_lists(
    island_images: list[IslandImage],
    output: Path,
    earlier_source_list: Path | None = None,
) -> Path | None:
    """Join the components of a round of island images, after those of earlier rounds if given"""
    source_lists = []
    tags = []
    if earlier_source_list is not None:
        source_lists.append(earlier_source_list)
        tags.append("")
    for island_image in island_images:
        if island_image.source_list is None:
            continue
        source_lists.append(island_image.source_list)
        tags.append(island_image.tag)

    return concatenate_source_lists(source_lists=source_lists, tags=tags, output=output)


def cube_channel_ranges(num_channels: int, cube_channels: int) -> list[tuple[int, int]]:
    """Split the channels of a measurement set as evenly as ``-channels-out``
    would. The end of each range is not part of it."""
    if not 1 < cube_channels <= num_channels:
        msg = f"{cube_channels=} has to be more than 1 and at most the {num_channels} channels of the data"
        raise ValueError(msg)

    return [
        (
            num_channels * channel // cube_channels,
            num_channels * (channel + 1) // cube_channels,
        )
        for channel in range(cube_channels)
    ]


@task
def task_restore_source_list(
    wsclean_result: WSCleanResult,
    source_list: Path,
    wsclean_container: Path,
    channel: bool = False,
) -> Path:
    """Add all components found to the image of what is left.

    With ``channel`` the image is one plane of a cube. One with no data is
    passed on as it is, and the images wsclean made are deleted once restored.
    """
    image_set = wsclean_result.image_set
    assert image_set is not None, "No images from wsclean"
    # The MFS image is the last
    residual_image = list(image_set.image)[-1]
    assert "cubletresidual" in residual_image.name, f"Unexpected {residual_image=}"
    output_image = residual_image.with_name(
        residual_image.name.replace("cubletresidual", "cubletrestored")
    )

    beam = fits.getheader(residual_image).get("BMAJ") if channel else None
    if channel and not (beam and np.isfinite(beam)):
        # Nothing to restore, but the cube still needs the plane
        logger.warning(f"{residual_image.name} has no beam, copying it as it is")
        shutil.copyfile(residual_image, output_image)
    else:
        restore_source_list(
            residual_image=residual_image,
            source_list=source_list,
            output_image=output_image,
            container=wsclean_container,
        )

    if channel:
        for image_type in ("image", "dirty", "psf", "model", "residual"):
            Path(f"{image_set.prefix}.{image_type}.fits").unlink(missing_ok=True)

    return output_image


@task
def task_combine_restored_channels(restored_images: list[Path]) -> Path:
    """Join the restored channel images into one cube, and delete them"""
    # The name of a channel image is <prefix>.ch<lo>-<hi>.image.fits
    prefix = restored_images[0].name.rsplit(".", maxsplit=3)[0]
    return combine_images_to_cube(
        images=restored_images,
        prefix=str(restored_images[0].parent / prefix),
        mode="image",
        fitscube_options=FitsCubeOptions(),
    )


@flow
def flow_beam_cublet_image(
    beam_cublet_options: BeamCubletOptions,
    crystalball_options: CrystalBallOptions | None = None,
) -> None:
    watch = watch_dask_cluster if beam_cublet_options.watch_cluster else nullcontext
    with watch():
        image_beam_cublets(
            beam_cublet_options=beam_cublet_options,
            crystalball_options=crystalball_options or CrystalBallOptions(),
        )


def save_cublet_settings(
    output_dir: Path,
    beam_cublet_options: BeamCubletOptions,
    crystalball_options: CrystalBallOptions,
) -> list[Path]:
    """Write timestamped copies of the strategy file and the options of a run to ``output_dir``"""
    return [
        copy_and_timestamp_strategy_file(
            output_dir=output_dir, input_yaml=beam_cublet_options.strategy
        ),
        dump_field_options_to_yaml(
            output_path=add_timestamp_to_path(
                input_path=output_dir / "beam_cublet_options.yaml"
            ),
            field_options=beam_cublet_options,
        ),
        dump_field_options_to_yaml(
            output_path=add_timestamp_to_path(
                input_path=output_dir / "cublet_crystalball_options.yaml"
            ),
            field_options=crystalball_options,
        ),
    ]


def image_beam_cublets(
    beam_cublet_options: BeamCubletOptions, crystalball_options: CrystalBallOptions
) -> None:
    """Image a beam island by island, in cycles of rounds"""
    ms = MS(path=beam_cublet_options.ms_path, column=beam_cublet_options.column)
    logger.info(f"Cublet imaging of {ms=}")
    # Worked out now, so that a bad channel count fails before any imaging
    channel_ranges = (
        cube_channel_ranges(
            num_channels=len(get_freqs_from_ms(ms=ms)),
            cube_channels=beam_cublet_options.cube_channels,
        )
        if beam_cublet_options.cube_channels
        else []
    )
    save_cublet_settings(
        output_dir=ms.path.parent,
        beam_cublet_options=beam_cublet_options,
        crystalball_options=crystalball_options,
    )

    strategy = load_strategy_yaml(input_yaml=beam_cublet_options.strategy)
    work_dir = beam_cublet_options.work_dir or ms.path.parent / "cublet"
    residual_columns = (
        beam_cublet_options.residual_column,
        f"{beam_cublet_options.residual_column}_ALT",
    )
    round_ms: Any = ms
    source_list: Path | None = None
    fits_mask: Any = None
    mfs_wsclean_options: dict[str, Any] = {}
    subtractions = 0

    for cycle in range(1, beam_cublet_options.cycles + 1):
        cycle_dir = work_dir / f"cycle{cycle}"
        mfs_wsclean_options = get_options_from_strategy(
            strategy=strategy, operation="cubletimage", mode="wsclean", round_info=cycle
        )
        if cycle > 1:
            mfs_wsclean_options["flint_name_suffix"] = f"cycle{cycle}"

        # Later cycles image what the earlier ones left
        mfs_result = task_wsclean_imager.submit(
            in_ms=round_ms,
            update_wsclean_options=mfs_wsclean_options,
            wsclean_container=beam_cublet_options.wsclean_container,
        )
        aegean_results = (
            task_run_bane_and_aegean.submit(
                image=mfs_result, aegean_container=beam_cublet_options.aegean_container
            )
            if beam_cublet_options.aegean_container
            else None
        )
        cycle_mask = task_create_image_mask_model.submit(
            image=mfs_result,
            image_products=aegean_results,
            update_masking_options=get_options_from_strategy(
                strategy=strategy,
                operation="cubletimage",
                mode="masking",
                round_info=cycle,
            ),
        )
        # An island of an earlier cycle stays open to correction
        fits_mask = (
            task_union_masks.submit(
                mask_names=cycle_mask,
                earlier_mask_names=fits_mask,
                output_dir=cycle_dir,
            )
            if fits_mask is not None
            else cycle_mask
        )
        regions = task_find_cublet_regions.submit(
            wsclean_result=mfs_result,
            fits_mask=fits_mask,
            output_dir=cycle_dir / "masks",
            beam_cublet_options=beam_cublet_options,
        ).result()
        if not regions:
            logger.warning(f"No islands found in cycle {cycle}, stopping")
            break

        island_wsclean_options = get_options_from_strategy(
            strategy=strategy,
            operation="cubletisland",
            mode="wsclean",
            round_info=cycle,
        )
        peak = regions[0].peak
        rounds = (
            beam_cublet_options.rounds
            if cycle == 1
            else beam_cublet_options.later_rounds
        )
        found_components = False

        for round_number in range(1, rounds + 1):
            threshold = round_threshold(
                peak=peak,
                round_gain=beam_cublet_options.round_gain,
                round_number=round_number,
                floor=beam_cublet_options.threshold_floor,
            )
            round_regions = [region for region in regions if region.peak > threshold]
            logger.info(
                f"Cycle {cycle} round {round_number}: {len(round_regions)} islands to {threshold=} Jy"
            )
            if not round_regions:
                break

            # The brightest island reorders the visibilities for the others
            round_dir = cycle_dir / f"round{round_number}"
            round_tag = f"c{cycle}r{round_number}"
            reuse = not beam_cublet_options.no_reuse_reordered
            island_results = []
            for region in round_regions:
                seed = island_results[0] if reuse and island_results else None
                is_seed = reuse and not island_results
                island_results.append(
                    task_image_island.submit(
                        ms=round_ms,
                        region=region,
                        wsclean_container=beam_cublet_options.wsclean_container,
                        update_wsclean_options=island_wsclean_options,
                        threshold=threshold,
                        name_suffix=f"{round_tag}i{region.idx:05d}",
                        temp_dir=round_dir
                        if is_seed
                        else round_dir / f"island{region.idx:05d}",
                        save_reordered=is_seed,
                        seed_dir=round_dir if seed else None,
                        keep_images=beam_cublet_options.keep_island_images,
                        wait_for=[seed] if seed else None,
                    )
                )

            round_source_list = task_concatenate_source_lists.submit(
                island_images=island_results,  # type: ignore[arg-type]
                output=work_dir / f"{ms.path.stem}.{round_tag}-sources.txt",
            ).result()
            task_remove_files_folders.submit(round_dir, wait_for=island_results)
            if round_source_list is None:
                logger.warning(f"No components found in {round_tag}, stopping")
                break
            found_components = True

            # Kept for the record. Only the components of this round are predicted
            source_list = task_concatenate_source_lists.submit(
                island_images=island_results,  # type: ignore[arg-type]
                output=work_dir / f"{ms.path.stem}.to-{round_tag}-sources.txt",
                earlier_source_list=source_list,
            ).result()

            # What is left of each island decides whether a later round images it
            imaged = {
                island_image.region.idx: island_image.region
                for island_image in (future.result() for future in island_results)
            }
            regions = [imaged.get(region.idx, region) for region in regions]

            predicted_ms = task_crystalball_to_ms.submit(
                ms=round_ms,
                crystalball_options=crystalball_options,
                wsclean_source_list_path=round_source_list,
            )
            # A rerun of the subtraction must not subtract twice, so it never
            # writes to the column it reads
            round_ms = task_subtract_model_from_ms.submit(
                ms=predicted_ms,
                output_column=residual_columns[subtractions % 2],
                update_tracked_column=True,
                chunk_size=1000,
            )
            subtractions += 1

        if not found_components:
            logger.warning(f"Cycle {cycle} changed nothing, stopping")
            break

    if source_list is None:
        return

    residual_wsclean_options = dict(mfs_wsclean_options)
    # wsclean only fits a beam, which the restoring needs, when it makes a PSF
    residual_wsclean_options.update(
        niter=0,
        make_psf=True,
        save_source_list=False,
        flint_name_suffix="cubletresidual",
    )
    residual_result = task_wsclean_imager.submit(
        in_ms=round_ms,
        update_wsclean_options=residual_wsclean_options,
        wsclean_container=beam_cublet_options.wsclean_container,
    )
    restored_mfs = task_restore_source_list.submit(
        wsclean_result=residual_result,
        source_list=source_list,
        wsclean_container=beam_cublet_options.wsclean_container,
    )

    if channel_ranges:
        # Calls that run at once must not share a directory, as wsclean names
        # its reordered files for the measurement set alone
        channel_temp_dir = residual_wsclean_options.get("temp_dir") or work_dir / "cube"
        restored_channels = []
        for channel, channel_range in enumerate(channel_ranges):
            channel_wsclean_options = dict(residual_wsclean_options)
            channel_wsclean_options.update(
                channels_out=1,
                join_channels=False,
                flint_name_suffix="cubletresidualchan",
                temp_dir=f"{channel_temp_dir}/channel{channel:04d}",
            )
            channel_result = task_wsclean_imager.submit(
                in_ms=round_ms,
                update_wsclean_options=channel_wsclean_options,
                wsclean_container=beam_cublet_options.wsclean_container,
                channel_range=channel_range,
            )
            restored_channels.append(
                task_restore_source_list.submit(
                    wsclean_result=channel_result,
                    source_list=source_list,
                    wsclean_container=beam_cublet_options.wsclean_container,
                    channel=True,
                )
            )
        task_combine_restored_channels.submit(
            restored_images=restored_channels  # type: ignore[arg-type]
        ).result()

    restored_mfs.result()


def setup_beam_cublet_workflow(
    beam_cublet_options: BeamCubletOptions,
    crystalball_options: CrystalBallOptions | None = None,
) -> None:
    dask_runner = get_dask_runner(cluster=beam_cublet_options.cluster_config)

    sbid = get_sbid_from_path(path=beam_cublet_options.ms_path)

    flow_beam_cublet_image.with_options(
        name=f"Flint Beam Cublet Image -- {sbid}", task_runner=dask_runner
    )(
        beam_cublet_options=beam_cublet_options,
        crystalball_options=crystalball_options,
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser = add_options_to_parser(parser=parser, options_class=BeamCubletOptions)
    parser = add_options_to_parser(parser=parser, options_class=CrystalBallOptions)

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    beam_cublet_options = create_options_from_parser(
        parser_namespace=args, options_class=BeamCubletOptions
    )

    crystalball_options = create_options_from_parser(
        parser_namespace=args, options_class=CrystalBallOptions
    )

    setup_beam_cublet_workflow(
        beam_cublet_options=beam_cublet_options,
        crystalball_options=crystalball_options,
    )


if __name__ == "__main__":
    cli()
