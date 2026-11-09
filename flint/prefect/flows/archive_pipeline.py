"""Archive one field's products to the destinations a strategy file defines.

The RACS-all flow runs this as its last stage. On its own it reruns the archive
of a field whose run failed, or sends it to destinations added since.
"""

from __future__ import annotations

from pathlib import Path

from configargparse import ArgumentParser
from prefect import flow, task
from prefect.futures import wait

from flint.archive import (
    archive_options_from_strategy,
    archive_to,
    check_archive_prerequisites,
)
from flint.naming import get_sbid_from_path
from flint.options import ArchiveOptions
from flint.prefect.clusters import get_dask_runner

task_archive_to = task(archive_to)


@flow(name="Flint Archive")
def process_archive(
    field_path: Path,
    archive_options: ArchiveOptions,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> None:
    """Send a field folder to every destination, each in its own task, and fail
    once all have had their go.

    Args:
        field_path (Path): The field folder, named by its SBID
        archive_options (ArchiveOptions): The destinations, from the strategy file
        rclone_container (Path | None, optional): The rclone container, needed by
            a ``copy`` destination. Defaults to None.
        rclone_config (Path | None, optional): The rclone config, needed by a
            remote. Defaults to None.

    Raises:
        RuntimeError: A destination was not reached
    """
    field = f"SB{get_sbid_from_path(path=field_path)}"
    futures = [
        task_archive_to.submit(
            root=field_path,
            field=field,
            destination=destination,
            rclone_container=rclone_container,
            rclone_config=rclone_config,
        )
        for destination in archive_options.destinations.values()
    ]
    wait(futures)

    failed = [
        name
        for name, future in zip(archive_options.destinations, futures)
        if not future.state.is_completed()
    ]
    if failed:
        raise RuntimeError(f"{field} did not reach {', '.join(failed)}")


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "field_path",
        type=Path,
        help="The processing folder of a field, named by its SBID",
    )
    parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        required=True,
        help="Strategy file whose defaults.archive.destinations say where the field goes",
    )
    parser.add_argument(
        "--rclone-container",
        type=Path,
        help="The rclone container, needed by every copy destination",
    )
    parser.add_argument(
        "--rclone-config",
        type=Path,
        help="rclone config file defining the remotes and holding their credentials",
    )
    parser.add_argument(
        "--cluster-config",
        type=str,
        default="petrichor",
        help="Path to a cluster configuration file, or a known cluster name",
    )

    return parser


def cli(argv: list[str] | None = None) -> None:
    args = get_parser().parse_args(argv)

    # The tasks run on another node, so the path has to stand on its own there
    field_path = args.field_path.resolve()
    if not field_path.is_dir():
        raise FileNotFoundError(f"The field folder {field_path} does not exist")
    archive_options = archive_options_from_strategy(
        strategy_yaml_path=args.strategy_yaml_path
    )
    check_archive_prerequisites(
        destinations=archive_options.destinations.values(),
        rclone_container=args.rclone_container,
        rclone_config=args.rclone_config,
    )
    process_archive.with_options(
        task_runner=get_dask_runner(cluster=args.cluster_config),
        name=f"Flint Archive -- {field_path.name}",
    )(
        field_path=field_path,
        archive_options=archive_options,
        rclone_container=args.rclone_container,
        rclone_config=args.rclone_config,
    )


if __name__ == "__main__":
    cli()
