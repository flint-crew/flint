"""Helper utility functions to download and otherwise manage
containers required for flint"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from flint.logging import logger
from flint.options import BaseOptions
from flint.sclient import pull_container


class FlintContainer(BaseOptions):
    """Item representing a Flint container"""

    name: str
    """Name of the container"""
    uri: str
    """URL of the container that can be used to pull with apptainer, e.g. docker://alecthomson/aoflagger:latest"""
    file_name: str
    """The expected file name of the container. This will be appended to the container directory path."""
    description: str | None = None
    """Short description on the purpose of the container"""


calibrate_container = FlintContainer(
    name="calibrate",
    file_name="flint-containers_calibrate.sif",
    uri="docker://alecthomson/flint-containers:calibrate",
    description="Contains AO calibrate and addmodel",
)
wsclean_container = FlintContainer(
    name="wsclean",
    file_name="flint-containers_wsclean.sif",
    uri="docker://alecthomson/flint-containers:wsclean",
    description="Container with the wsclean deconvolution software",
)
askapsoft_contaer = FlintContainer(
    name="askapsoft",
    file_name="flint-containers_askapsoft.sif",
    uri="docker://alecthomson/flint-containers:askapsoft",
    description="Container with askapsoft (also known as yandasoft)",
)
aoflagger_contaer = FlintContainer(
    name="aoflagger",
    file_name="flint-containers_aoflagger.sif",
    uri="docker://alecthomson/flint-containers:aoflagger",
    description="Container with aoflagger, used to autonomously flag measurement sets",
)
aegean_contaer = FlintContainer(
    name="aegean",
    file_name="flint-containers_aegean.sif",
    uri="docker://alecthomson/flint-containers:aegean",
    description="Container with aegean, used to source find",
)
potato_container = FlintContainer(
    name="potato",
    file_name="flint-containers_potato.sif",
    uri="docker://alecthomson/flint-containers:potato",
    description="Peel out that terrible annoying source",
)

LIST_OF_KNOWN_CONTAINERS = (
    calibrate_container,
    wsclean_container,
    askapsoft_contaer,
    aoflagger_contaer,
    aegean_contaer,
    potato_container,
)
KNOWN_CONTAINER_LOOKUP: dict[str, FlintContainer] = {
    v.name: v for v in LIST_OF_KNOWN_CONTAINERS
}


def log_known_containers() -> None:
    """Log the known containers. This simply prints the set of known containers."""

    for idx, known_container in enumerate(LIST_OF_KNOWN_CONTAINERS):
        logger.info(f"Container {idx + 1} of {len(LIST_OF_KNOWN_CONTAINERS)}")
        logger.info(f"  Name: {known_container.name}")
        logger.info(f"  File name: {known_container.file_name}")
        logger.info(f"  URL: {known_container.uri}")
        logger.info(f"  Description: {known_container.description}")


def get_known_container_path(container_directory: Path | str, name: str) -> Path:
    """Return the path to a ``flint`` known container. These are containers that
    are downloaded through the ``download_known_containers`` function.

    Args:
        container_directory (Path | str): Path to directory containing downloaded containers
        name (str): Name of the container. Note that this is not the file name.

    Raises:
        ValueError: Raised when the name is not known

    Returns:
        Path: Path to the requested
    """

    container_directory = Path(container_directory)

    known_container = KNOWN_CONTAINER_LOOKUP.get(name, None)

    if known_container is None:
        raise ValueError(
            f"{name=} is not known. See {list(KNOWN_CONTAINER_LOOKUP.keys())}"
        )

    known_container_path = container_directory / known_container.file_name
    assert known_container_path.exists(), (
        f"{known_container_path=} of {name=} does not exist"
    )

    return known_container_path


def verify_known_containers(container_directory: Path | str) -> bool:
    """Inspect the provided ``container_directory`` to examine that the set of
    containers that are expected to exist are present.

    Args:
        container_directory (Path | str): Directory to search that should have containers

    Returns:
        bool: True is all containers are available. False otherwise.
    """

    logger.info(f"Checking {container_directory=} for known containers")
    container_valid = {}
    for known_container in LIST_OF_KNOWN_CONTAINERS:
        try:
            _ = get_known_container_path(
                container_directory=container_directory, name=known_container.name
            )
            valid = True
        except (ValueError, AssertionError):
            valid = False

        logger.info(
            f"Container {known_container.name} is {'valid' if valid else 'not valid'}"
        )
        container_valid[known_container.name] = valid

    return all(container_valid.values())


def download_known_containers(container_directory: Path | str) -> tuple[Path, ...]:
    """Download known containers for use throughout flint.

    Args:
        container_directory (Path | str): Output directory to store containers. Will be created if necessary.

    Returns:
        tuple[Path, ...]: Paths to all containers downloaded
    """
    container_directory = Path(container_directory)

    if not container_directory.exists():
        logger.info(f"Creating {container_directory=}")
        container_directory.mkdir(parents=True)

    containers_downloaded = []
    for idx, known_container in enumerate(LIST_OF_KNOWN_CONTAINERS):
        logger.info(
            f"Downloading {idx + 1} of {len(LIST_OF_KNOWN_CONTAINERS)}, container {known_container.name}"
        )

        expected_output_path = container_directory / known_container.file_name

        if expected_output_path.exists():
            logger.info(f"{expected_output_path=} already exists. Skipping.")
            continue

        _container_path = pull_container(
            container_directory=container_directory,
            uri=known_container.uri,
            file_name=known_container.file_name,
        )
        if not expected_output_path.exists():
            logger.error(
                f"{expected_output_path=} but was not. Instead received {_container_path=}"
            )

        containers_downloaded.append(expected_output_path)

    logger.info(f"Downloaded {len(containers_downloaded)} new containers")
    return tuple(containers_downloaded)


def get_parser() -> ArgumentParser:
    """Create the CLI argument parser

    Returns:
        ArgumentParser: Constructed argument parser
    """
    parser = ArgumentParser(description="Download and manage Flint containers")
    subparsers = parser.add_subparsers(dest="mode")

    _ = subparsers.add_parser(
        name="list", help="List the containers that are known to Flint"
    )

    download_parser = subparsers.add_parser(
        name="download", help="Pull each of the known containers"
    )

    download_parser.add_argument(
        "container_directory", type=Path, help="Location to download containers to"
    )

    verify_parser = subparsers.add_parser(
        name="verify", help="Pull each of the known containers"
    )

    verify_parser.add_argument(
        "container_directory", type=Path, help="Location to download containers to"
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "list":
        log_known_containers()
    elif args.mode == "download":
        download_known_containers(container_directory=args.container_directory)
    elif args.mode == "verify":
        verify_known_containers(container_directory=args.container_directory)
    else:
        logger.info(f"Unknown directive: {args.mode}")


if __name__ == "__main__":
    cli()
