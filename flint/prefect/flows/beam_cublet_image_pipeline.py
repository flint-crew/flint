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
    union_masks,
)
from flint.cublet.reordered import link_reordered_files
from flint.cublet.sourcelist import concatenate_source_lists
from flint.imager.wsclean import WSCleanResult, restore_source_list
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

    if not keep_images:
        # By name, as flint leaves images on disk that it does not gather
        prefix = Path(image_set.prefix)
        for image in prefix.parent.glob(f"{prefix.name}.*.fits"):
            image.unlink()

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


@task
def task_restore_source_list(
    wsclean_result: WSCleanResult, source_list: Path, wsclean_container: Path
) -> Path:
    """Add all components found to the MFS image of what is left"""
    assert wsclean_result.image_set is not None, "No images from wsclean"
    # The MFS image is the last
    residual_image = list(wsclean_result.image_set.image)[-1]
    assert "cubletresidual" in residual_image.name, f"Unexpected {residual_image=}"
    return restore_source_list(
        residual_image=residual_image,
        source_list=source_list,
        output_image=residual_image.with_name(
            residual_image.name.replace("cubletresidual", "cubletrestored")
        ),
        container=wsclean_container,
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


def image_beam_cublets(
    beam_cublet_options: BeamCubletOptions, crystalball_options: CrystalBallOptions
) -> None:
    """Image a beam island by island, in cycles of rounds"""
    ms = MS(path=beam_cublet_options.ms_path, column=beam_cublet_options.column)
    logger.info(f"Cublet imaging of {ms=}")

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
    task_restore_source_list.submit(
        wsclean_result=residual_result,
        source_list=source_list,
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
