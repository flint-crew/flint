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

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser
from eye_patch.naming import FITSMaskNames
from prefect import flow

from flint.configuration import get_options_from_strategy, load_strategy_yaml
from flint.cublet.islands import (
    CubletRegion,
    find_cublet_regions,
    masked_peak,
    round_threshold,
)
from flint.cublet.reordered import link_reordered_files
from flint.cublet.sourcelist import concatenate_source_lists
from flint.imager.wsclean import WSCleanResult
from flint.logging import logger
from flint.ms import MS
from flint.naming import get_sbid_from_path
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
    """The column the data less the components of all islands is written to. Even rounds write to a second column with an ``_ALT`` suffix, so that no round subtracts from the column it reads"""
    rounds: int = 3
    """Number of times every island is imaged, with the components subtracted in between"""
    round_gain: float = 0.97
    """Fraction of the brightest island that each round cleans away. One less the far sidelobe level of the PSF is as deep as a round can usefully go"""
    threshold_floor: float = 0.0
    """Islands are never cleaned below this, in Jy"""
    merge_pixels: int = 0
    """Islands closer than twice this many pixels are imaged as one"""
    pad_factor: float = 2.5
    """Size of an island image over the size of its island"""
    min_size: int = 128
    """Smallest island image, in pixels"""
    max_islands: int | None = None
    """Only image this many of the brightest islands"""
    no_reuse_reordered: bool = False
    """Have every island reorder the visibilities for itself, rather than share those of the brightest island of the round"""
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
) -> IslandImage:
    """Image one island. With a ``seed_dir`` its reordered visibilities are used."""
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
        flint_hold_dir=ms.path.parent,
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
        )
    finally:
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


def image_beam_cublets(
    beam_cublet_options: BeamCubletOptions, crystalball_options: CrystalBallOptions
) -> None:
    """Image a beam island by island, in rounds"""
    ms = MS(path=beam_cublet_options.ms_path, column=beam_cublet_options.column)
    logger.info(f"Cublet imaging of {ms=}")

    strategy = load_strategy_yaml(input_yaml=beam_cublet_options.strategy)

    mfs_wsclean_options = get_options_from_strategy(
        strategy=strategy, operation="cubletimage", mode="wsclean"
    )

    work_dir = beam_cublet_options.work_dir or ms.path.parent / "cublet"

    mfs_result = task_wsclean_imager.submit(
        in_ms=ms,
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
    fits_mask = task_create_image_mask_model.submit(
        image=mfs_result,
        image_products=aegean_results,
        update_masking_options=get_options_from_strategy(
            strategy=strategy, operation="cubletimage", mode="masking"
        ),
    )
    regions = task_find_cublet_regions.submit(
        wsclean_result=mfs_result,
        fits_mask=fits_mask,
        output_dir=work_dir / "masks",
        beam_cublet_options=beam_cublet_options,
    ).result()
    if not regions:
        logger.warning("No islands found, nothing to image")
        return

    island_wsclean_options = get_options_from_strategy(
        strategy=strategy, operation="cubletisland", mode="wsclean"
    )
    peak = regions[0].peak
    residual_columns = (
        beam_cublet_options.residual_column,
        f"{beam_cublet_options.residual_column}_ALT",
    )
    round_ms: Any = ms
    source_list: Path | None = None

    for round_number in range(1, beam_cublet_options.rounds + 1):
        threshold = round_threshold(
            peak=peak,
            round_gain=beam_cublet_options.round_gain,
            round_number=round_number,
            floor=beam_cublet_options.threshold_floor,
        )
        round_regions = [region for region in regions if region.peak > threshold]
        logger.info(
            f"Round {round_number}: {len(round_regions)} islands to {threshold=} Jy"
        )
        if not round_regions:
            break

        # The brightest island reorders the visibilities for the others
        round_dir = work_dir / f"round{round_number}"
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
                    name_suffix=f"r{round_number}i{region.idx:05d}",
                    temp_dir=round_dir
                    if is_seed
                    else round_dir / f"island{region.idx:05d}",
                    save_reordered=is_seed,
                    seed_dir=round_dir if seed else None,
                    wait_for=[seed] if seed else None,
                )
            )

        round_source_list = task_concatenate_source_lists.submit(
            island_images=island_results,  # type: ignore[arg-type]
            output=work_dir / f"{ms.path.stem}.round{round_number}-sources.txt",
        ).result()
        task_remove_files_folders.submit(round_dir, wait_for=island_results)
        if round_source_list is None:
            logger.warning(f"No components found in round {round_number}, stopping")
            break

        # Kept for the record. Only the components of this round are predicted
        source_list = task_concatenate_source_lists.submit(
            island_images=island_results,  # type: ignore[arg-type]
            output=work_dir / f"{ms.path.stem}.rounds1to{round_number}-sources.txt",
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
            output_column=residual_columns[(round_number - 1) % 2],
            update_tracked_column=True,
            chunk_size=1000,
        )

    if source_list is None:
        return

    residual_wsclean_options = dict(mfs_wsclean_options)
    residual_wsclean_options.update(
        niter=0, save_source_list=False, flint_name_suffix="cubletresidual"
    )
    task_wsclean_imager.submit(
        in_ms=round_ms,
        update_wsclean_options=residual_wsclean_options,
        wsclean_container=beam_cublet_options.wsclean_container,
    ).result()


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
