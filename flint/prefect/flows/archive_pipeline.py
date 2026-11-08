"""Archive a field's products to every destination its manifest lists.

Runs as the last RACS-all stage, or on its own from the manifest of a finished run:
to retry a failed archive, or to add a destination to a field archived earlier.
``check`` tells a purge whether every destination holds the field.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from datetime import UTC, datetime
from pathlib import Path

from prefect import flow, unmapped
from prefect.futures import wait

from flint.archive import (
    archive_destinations,
    archive_to,
    check_archive,
    check_archive_prerequisites,
)
from flint.configuration import get_options_from_strategy
from flint.manifest import (
    ArchiveManifest,
    add_destinations,
    read_manifest,
    seal_product,
    write_manifest,
)
from flint.options import ArchiveOptions
from flint.prefect.caching import task
from flint.prefect.clusters import get_dask_runner
from flint.prefect.common.utils import raise_on_failed_stage

task_seal_product = task(seal_product)
# A tag concurrency limit on flint-archive caps how many copies run across all fields
task_archive_to = task(archive_to, tags=["flint-archive"])


@flow(name="Flint Archive")
def process_archive(
    manifest_path: Path,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> ArchiveManifest:
    """Seal a field's manifest if it is not yet, then archive the field to each
    destination it lists. One failed destination does not stop the others."""
    manifest = read_manifest(manifest_path)

    if manifest.sealed is None:
        sealed_products = task_seal_product.map(
            root=unmapped(manifest.root), product=list(manifest.products)
        )
        wait(sealed_products)
        raise_on_failed_stage(stage="archive sealing", futures=list(sealed_products))
        manifest = manifest.with_options(
            products=tuple(product.result() for product in sealed_products),
            sealed=datetime.now(UTC),
        )
        write_manifest(manifest=manifest, path=manifest_path)

    arguments = dict(
        manifest=manifest,
        rclone_container=rclone_container,
        rclone_config=rclone_config,
    )
    futures = [
        task_archive_to.submit(destination=destination, **arguments)
        for destination in manifest.destinations
        if destination.name in ("copy", "tar")
    ]
    for destination in manifest.destinations:
        if destination.name not in ("copy", "tar"):
            # One remote at a time, so remotes do not share the bandwidth. Chaining
            # them with wait_for would stop every later remote once one failed.
            future = task_archive_to.submit(destination=destination, **arguments)
            future.wait()
            futures.append(future)

    wait(futures)
    raise_on_failed_stage(stage="archive", futures=futures)

    return manifest


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Archive a field to the destinations its manifest lists"
    )
    run_parser.add_argument("manifest", type=Path, help="The field's archive manifest")
    run_parser.add_argument(
        "--copy-path", type=Path, default=None, help="Add a hot copy under this folder"
    )
    run_parser.add_argument(
        "--tar-path", type=Path, default=None, help="Add a tarball in this folder"
    )
    run_parser.add_argument(
        "--archive-remotes",
        nargs="+",
        default=[],
        help="Add these remotes, as named in the strategy file",
    )
    run_parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        default=None,
        help="Strategy file whose defaults.archive defines the remotes",
    )
    run_parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name",
    )

    check_parser = subparsers.add_parser(
        "check", help="Exit with 1 unless every destination holds the field"
    )
    check_parser.add_argument(
        "manifest", type=Path, help="The field's archive manifest"
    )

    for subparser in (run_parser, check_parser):
        subparser.add_argument(
            "--rclone-container", type=Path, default=None, help="The rclone container"
        )
        subparser.add_argument(
            "--rclone-config",
            type=Path,
            default=None,
            help="rclone config file defining the remotes",
        )

    return parser


def cli(argv: list[str] | None = None) -> None:
    args = get_parser().parse_args(argv)
    manifest = read_manifest(args.manifest)

    if args.mode == "check":
        check_archive_prerequisites(
            destinations=manifest.destinations,
            rclone_container=args.rclone_container,
            rclone_config=args.rclone_config,
        )
        complete = check_archive(
            manifest=manifest,
            rclone_container=args.rclone_container,
            rclone_config=args.rclone_config,
        )
        sys.exit(0 if complete else 1)

    remotes = ArchiveOptions(
        **get_options_from_strategy(
            strategy=args.strategy_yaml_path,
            operation="selfcal",
            mode="archive",
            round_info=0,
        )
    ).remotes
    manifest = add_destinations(
        manifest=manifest,
        destinations=archive_destinations(
            copy_path=args.copy_path,
            tar_path=args.tar_path,
            remote_names=args.archive_remotes,
            remotes=remotes,
        ),
    )
    check_archive_prerequisites(
        destinations=manifest.destinations,
        rclone_container=args.rclone_container,
        rclone_config=args.rclone_config,
    )
    write_manifest(manifest=manifest, path=args.manifest)

    process_archive.with_options(
        task_runner=get_dask_runner(cluster=args.cluster_config)
    )(
        manifest_path=args.manifest,
        rclone_container=args.rclone_container,
        rclone_config=args.rclone_config,
    )


if __name__ == "__main__":
    cli()
