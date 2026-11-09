"""Archive one field's products to the destinations a strategy file defines.

The RACS-all flow runs this as its last stage. On its own it reruns the archive
of a field whose run failed, or sends it to destinations added since.
"""

from __future__ import annotations

from capn_crunch import add_options_to_parser, create_options_from_parser
from configargparse import ArgumentParser
from prefect import flow, task
from prefect.futures import wait

from flint.archive import (
    archive_options_from_strategy,
    archive_to,
    check_archive_prerequisites,
)
from flint.naming import get_sbid_from_path
from flint.options import ArchiveFieldOptions, ArchiveOptions
from flint.prefect.clusters import get_dask_runner

task_archive_to = task(archive_to)


@flow(name="Flint Archive")
def process_archive(
    archive_field_options: ArchiveFieldOptions,
    archive_options: ArchiveOptions,
) -> None:
    """Send a field folder to every destination, each in its own task, and fail
    once all have had their go.

    Args:
        archive_field_options (ArchiveFieldOptions): The field folder, and the
            rclone container and config its destinations need
        archive_options (ArchiveOptions): The destinations, from the strategy file

    Raises:
        RuntimeError: A destination was not reached
    """
    field_path = archive_field_options.field_path
    assert field_path is not None, "field_path is required"
    field = f"SB{get_sbid_from_path(path=field_path)}"
    futures = [
        task_archive_to.submit(
            root=field_path,
            field=field,
            destination=destination,
            rclone_container=archive_field_options.rclone_container,
            rclone_config=archive_field_options.rclone_config,
        )
        for destination in archive_options.destinations.values()
    ]
    wait(futures)

    failed = [
        f"{name}: {future.state.message}"
        for name, future in zip(archive_options.destinations, futures)
        if not future.state.is_completed()
    ]
    if failed:
        raise RuntimeError(
            f"{field} did not reach every destination. " + " ".join(failed)
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
        help="Path to a cluster configuration file, or a known cluster name",
    )
    parser = add_options_to_parser(
        parser=parser,
        options_class=ArchiveFieldOptions,
        description="Archive options",
    )

    return parser


def cli(argv: list[str] | None = None) -> None:
    args = get_parser().parse_args(argv)
    options = create_options_from_parser(
        parser_namespace=args, options_class=ArchiveFieldOptions
    )
    if options.field_path is None or options.imaging_strategy is None:
        raise ValueError("--field-path and --imaging-strategy are both required")

    # The tasks run on another node, so the path has to stand on its own there
    field_path = options.field_path.resolve()
    if not field_path.is_dir():
        raise FileNotFoundError(f"The field folder {field_path} does not exist")
    options = options.with_options(field_path=field_path)
    archive_options = archive_options_from_strategy(
        strategy_yaml_path=options.imaging_strategy
    )
    check_archive_prerequisites(
        destinations=archive_options.destinations.values(),
        rclone_container=options.rclone_container,
        rclone_config=options.rclone_config,
    )
    process_archive.with_options(
        task_runner=get_dask_runner(cluster=args.cluster_config),
        name=f"Flint Archive -- {field_path.name}",
    )(archive_field_options=options, archive_options=archive_options)


if __name__ == "__main__":
    cli()
