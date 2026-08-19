"""Standalone SPICE-style cube compression pipeline: trim already-imaged
Stokes cubes down to small boxes around catalogued sources, crop to their
union, and compress. See ``flint.spice``.
"""

from __future__ import annotations

from pathlib import Path

from astropy.io import fits
from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from prefect import flow, task, unmapped
from radio_beam import Beam

from flint.configuration import get_options_from_strategy, load_and_copy_strategy
from flint.convol import BeamShape
from flint.logging import logger
from flint.naming import get_sbid_from_path
from flint.options import SpiceFieldOptions, SpiceOptions
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.spice import (
    task_compress_cube,
    task_get_spice_boxes,
    task_spice_fits,
)
from flint.prefect.common.utils import task_archive_sbid, task_getattr
from flint.source_finding.aegean import run_bane_and_aegean
from flint.spice import any_box_overlaps

task_run_bane_and_aegean = task(run_bane_and_aegean)


@flow(name="Flint SPICE Compression Pipeline")
def process_spice_compression(spice_field_options: SpiceFieldOptions) -> list[Path]:
    if not spice_field_options.cubes:
        raise ValueError(
            "``cubes`` is required. The racs-all flow sets it from the polarisation stage."
        )

    strategy = load_and_copy_strategy(
        output_split_science_path=spice_field_options.cubes[0].parent,
        imaging_strategy=spice_field_options.imaging_strategy,
    )

    is_user_catalogue = spice_field_options.catalogue is not None
    if is_user_catalogue:
        catalogue_path = spice_field_options.catalogue
        reference_image = spice_field_options.cubes[0]
    else:
        assert spice_field_options.reference_image is not None, (
            "reference_image is required when no catalogue is supplied "
            "(built-in aegean source finding)"
        )
        assert spice_field_options.aegean_container is not None, (
            "aegean_container is required when no catalogue is supplied"
        )
        reference_image = spice_field_options.reference_image
        aegean_outputs = task_run_bane_and_aegean.submit(
            image=reference_image,
            aegean_container=spice_field_options.aegean_container,
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

    beam_shape = BeamShape.from_radio_beam(
        Beam.from_fits_header(fits.getheader(reference_image))
    )

    island_sky_boxes = task_get_spice_boxes.submit(
        reference_image=reference_image,
        catalogue=catalogue_path,
        spice_options=spice_options,
        beam_shape=beam_shape,
        is_user_catalogue=is_user_catalogue,
    )

    resolved_sky_boxes = island_sky_boxes.result()
    overlapping = [
        cube
        for cube in spice_field_options.cubes
        if any_box_overlaps(fits_path=cube, sky_boxes=resolved_sky_boxes)
    ]
    if not overlapping:
        raise ValueError(
            f"No island overlaps any of the {len(spice_field_options.cubes)} supplied "
            "cubes. Check the catalogue and reference image match the field."
        )
    if len(overlapping) != len(spice_field_options.cubes):
        logger.warning(
            f"{len(spice_field_options.cubes) - len(overlapping)} cube(s) have no "
            "islands and will be compressed without spicing"
        )

    spiced_cubes = task_spice_fits.map(
        fits_path=spice_field_options.cubes,
        sky_boxes=unmapped(resolved_sky_boxes),
        output_path=unmapped(spice_field_options.output_path),
    )

    compressed_cubes = task_compress_cube.map(
        out_cube=spiced_cubes,
        method=unmapped(spice_options.compress_method),
        max_workers=unmapped(spice_options.compress_max_workers),
    )

    logger.info(f"Compressed {len(compressed_cubes)} cubes")
    written_paths = compressed_cubes.result()

    if spice_field_options.sbid_copy_path:
        task_archive_sbid.submit(
            science_folder_path=spice_field_options.output_path
            or spice_field_options.cubes[0].parent,
            copy_path=spice_field_options.sbid_copy_path,
        ).result()

    return written_paths


def setup_run_spice_compression(
    cluster_config: str | Path, spice_field_options: SpiceFieldOptions
) -> None:
    if spice_field_options.sbid_copy_path and spice_field_options.cubes:
        science_sbid = get_sbid_from_path(path=spice_field_options.cubes[0])
        spice_field_options = spice_field_options.with_options(
            sbid_copy_path=spice_field_options.sbid_copy_path / f"{science_sbid}"
        )

    dask_task_runner = get_dask_runner(cluster=cluster_config)

    process_spice_compression.with_options(task_runner=dask_task_runner)(
        spice_field_options=spice_field_options
    )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "--cli-config", is_config_file=True, help="Path to configuration file"
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name. ",
    )

    parser = add_options_to_parser(
        parser=parser,
        options_class=SpiceFieldOptions,
        description="SPICE compression processing options",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    spice_field_options = create_options_from_parser(
        parser_namespace=args, options_class=SpiceFieldOptions
    )

    setup_run_spice_compression(
        cluster_config=args.cluster_config,
        spice_field_options=spice_field_options,
    )


if __name__ == "__main__":
    cli()
