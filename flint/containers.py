"""Helper utility functions to download and otherwise manage
containers required for flint"""

from __future__ import annotations

import asyncio
import platform
from argparse import ArgumentParser
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypeAlias, get_args

from capn_crunch import BaseOptions

from flint.logging import logger
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
    tag: str = "v1.0"
    """Tag of the container, if applicable"""
    fixed_tag: bool = False
    """Keep ``tag`` as it is, for an upstream image without flint's release and CPU tags"""


calibrate_container = FlintContainer(
    name="calibrate",
    file_name="flint-containers_calibrate.sif",
    uri="docker://ghcr.io/flint-crew/calibrate",
    description="Contains AO calibrate and addmodel",
)
wsclean_container = FlintContainer(
    name="wsclean",
    file_name="flint-containers_wsclean.sif",
    uri="docker://ghcr.io/flint-crew/wsclean",
    description="Container with the wsclean deconvolution software",
)
askapsoft_contaer = FlintContainer(
    name="askapsoft",
    file_name="flint-containers_askapsoft.sif",
    uri="docker://ghcr.io/flint-crew/askapsoft",
    description="Container with askapsoft (also known as yandasoft)",
)
aoflagger_contaer = FlintContainer(
    name="aoflagger",
    file_name="flint-containers_aoflagger.sif",
    uri="docker://ghcr.io/flint-crew/aoflagger",
    description="Container with aoflagger, used to autonomously flag measurement sets",
)
aegean_contaer = FlintContainer(
    name="aegean",
    file_name="flint-containers_aegean.sif",
    uri="docker://ghcr.io/flint-crew/aegean",
    description="Container with aegean, used to source find",
)
potato_container = FlintContainer(
    name="potato",
    file_name="flint-containers_potato.sif",
    uri="docker://ghcr.io/flint-crew/potato",
    description="Peel out that terrible annoying source",
)
casa_container = FlintContainer(
    name="casa",
    file_name="flint-containers_casa.sif",
    uri="docker://ghcr.io/flint-crew/casa",
    description="A monolithic install of the CASA application",
)
rclone_container = FlintContainer(
    name="rclone",
    file_name="rclone.sif",
    uri="docker://rclone/rclone",
    description="rclone, used to copy archived products and check them",
    tag="1.75.2",
    fixed_tag=True,
)
LIST_OF_KNOWN_CONTAINERS = (
    calibrate_container,
    wsclean_container,
    askapsoft_contaer,
    aoflagger_contaer,
    aegean_contaer,
    potato_container,
    casa_container,
    rclone_container,
)
KNOWN_CONTAINER_LOOKUP: dict[str, FlintContainer] = {
    v.name: v for v in LIST_OF_KNOWN_CONTAINERS
}

CpuLevel: TypeAlias = Literal["portable", "x86-64-v3", "x86-64-v4"]
"""CPU level the containers are built for. Each non-portable level is a tag suffix."""


def _sanity_check_containers(
    container_list: list[FlintContainer] | tuple[FlintContainer, ...],
) -> None:
    """Do some quick validation checks on the set of loaded containers. Make sure there are no
    duplicated names or file names"""
    assert len(container_list) == len(
        set([container.name for container in container_list])
    ), "Duplicated name detected in reference containers"
    assert len(container_list) == len(
        set([container.file_name for container in container_list])
    ), "Duplicated file name detected in reference containers"


_sanity_check_containers(container_list=LIST_OF_KNOWN_CONTAINERS)


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


def parse_cpuinfo_flags(cpuinfo: str) -> set[str]:
    """Return the CPU flags from the text of ``/proc/cpuinfo``, or an empty set if it has none."""
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "flags":
            return set(value.split())
    return set()


def cpu_level_from_flags(flags: Iterable[str]) -> CpuLevel:
    """Return the highest CPU level that a set of ``/proc/cpuinfo`` flags supports."""
    flags = set(flags)
    # x86-64-v2 and v3 features as /proc/cpuinfo names them (LZCNT shows up as abm)
    v3_flags = {
        *("cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"),
        *("avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"),
    }
    v4_flags = {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}

    if not v3_flags <= flags:
        return "portable"
    if not v4_flags <= flags:
        return "x86-64-v3"
    return "x86-64-v4"


def detect_cpu_level(
    cpuinfo_path: Path = Path("/proc/cpuinfo"), machine: str | None = None
) -> CpuLevel:
    """Return the highest CPU level this machine supports, or ``portable`` if unsure.

    Args:
        cpuinfo_path (Path, optional): File to read the CPU flags from. Defaults to ``/proc/cpuinfo``.
        machine (str | None, optional): Machine architecture. Defaults to ``platform.machine()``.

    Returns:
        CpuLevel: The detected CPU level
    """
    machine = machine or platform.machine()
    if machine.lower() not in ("x86_64", "amd64"):
        return "portable"

    try:
        cpuinfo = Path(cpuinfo_path).read_text()
    except OSError as error:
        logger.warning(f"Could not read {cpuinfo_path}, using portable: {error}")
        return "portable"

    return cpu_level_from_flags(parse_cpuinfo_flags(cpuinfo))


def resolve_cpu_level(cpu: CpuLevel | Literal["auto"]) -> CpuLevel:
    """Turn a ``--cpu`` choice into a CPU level, detecting it when ``cpu`` is ``auto``."""
    if cpu == "auto":
        cpu_level = detect_cpu_level()
        logger.info(f"Detected CPU level {cpu_level}")
        return cpu_level
    if cpu not in get_args(CpuLevel):
        raise ValueError(f"{cpu=} is not known. See {get_args(CpuLevel)} or 'auto'")
    return cpu


def get_container_tag(tag: str, cpu_level: CpuLevel) -> str:
    """Return the container tag for a release ``tag`` built for ``cpu_level``."""
    return tag if cpu_level == "portable" else f"{tag}-{cpu_level}"


async def _pull_and_check_container(
    container_directory: Path,
    known_container: FlintContainer,
    expected_output_path: Path,
) -> Path:
    """Pull a container and check that it was downloaded correctly

    Args:
        container_directory (Path): Output directory to store containers. Will be created if necessary.
        known_container (FlintContainer): Container to download
        expected_output_path (Path): Expected output path

    Returns:
        Path: The expected path of the container
    """
    _container_path = await asyncio.to_thread(
        pull_container,
        container_directory=container_directory,
        uri=f"{known_container.uri}:{known_container.tag}",
        file_name=known_container.file_name,
    )
    if not expected_output_path.exists():
        logger.error(
            f"{expected_output_path=} but was not. Instead received {_container_path=}"
        )

    return expected_output_path


async def download_known_containers_coro(
    container_directory: Path | str,
    new_tag: str | None = None,
    cpu: CpuLevel | Literal["auto"] = "portable",
) -> tuple[Path, ...]:
    """Download known containers for use throughout flint.

    Args:
        container_directory (Path | str): Output directory to store containers. Will be created if necessary.
        new_tag (str, optional): The tag associated with the containers to download. If None the latest image will be downloaded. Defaults to None.
        cpu (str, optional): CPU level to download the containers for, a ``CpuLevel`` or ``auto``. Defaults to "portable".

    Returns:
        tuple[Path, ...]: Paths to all containers downloaded
    """
    container_directory = Path(container_directory)
    cpu_level = resolve_cpu_level(cpu)
    cpu_levels = get_args(CpuLevel)
    if cpu_levels.index(cpu_level) > cpu_levels.index(detect_cpu_level()):
        logger.warning(f"{cpu_level} containers will not run on this machine's CPU")

    if not container_directory.exists():
        logger.info(f"Creating {container_directory=}")
        container_directory.mkdir(parents=True)

    containers_downloaded = []
    coros = []
    for idx, known_container in enumerate(LIST_OF_KNOWN_CONTAINERS):
        if not known_container.fixed_tag:
            known_container = known_container.with_options(
                tag=get_container_tag(
                    tag=new_tag or known_container.tag, cpu_level=cpu_level
                )
            )
        logger.info(
            f"Downloading {idx + 1} of {len(LIST_OF_KNOWN_CONTAINERS)}, container {known_container.name}, tag {known_container.tag}"
        )

        expected_output_path = container_directory / known_container.file_name

        if expected_output_path.exists():
            logger.info(f"{expected_output_path=} already exists. Skipping.")
            continue

        coro = _pull_and_check_container(
            container_directory=container_directory,
            known_container=known_container,
            expected_output_path=expected_output_path,
        )
        coros.append(coro)

        containers_downloaded.append(expected_output_path)

    await asyncio.gather(*coros)

    logger.info(f"Downloaded {len(containers_downloaded)} new containers")
    return tuple(containers_downloaded)


def download_known_containers(
    container_directory: Path | str,
    new_tag: str | None = None,
    cpu: CpuLevel | Literal["auto"] = "portable",
) -> tuple[Path, ...]:
    """Download known containers for use throughout flint. This
    calls the async enabled download function and blocks until results
    are gathered.

    Args:
        container_directory (Path | str): Output directory to store containers. Will be created if necessary.
        new_tag (str, optional): The tag associated with the containers to download. If None the latest image will be downloaded. Defaults to None.
        cpu (str, optional): CPU level to download the containers for, a ``CpuLevel`` or ``auto``. Defaults to "portable".

    Returns:
        tuple[Path, ...]: Paths to all containers downloaded
    """
    return asyncio.run(
        download_known_containers_coro(
            container_directory=container_directory, new_tag=new_tag, cpu=cpu
        )
    )


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
    download_parser.add_argument(
        "--tag", type=str, help="Tag of the containers to download", default=None
    )
    download_parser.add_argument(
        "--cpu",
        choices=(*get_args(CpuLevel), "auto"),
        default="portable",
        help=(
            "CPU level to download the containers for. 'portable' runs on any 64-bit x86 or ARM CPU, "
            "'x86-64-v3' needs AVX2 and 'x86-64-v4' needs AVX-512. 'auto' picks the highest level "
            "the machine running this command supports, so on a cluster run it on a compute node, "
            "not the login node. Existing container files are not replaced."
        ),
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
        download_known_containers(
            container_directory=args.container_directory,
            new_tag=args.tag,
            cpu=args.cpu,
        )

    elif args.mode == "verify":
        verify_known_containers(container_directory=args.container_directory)
    else:
        logger.info(f"Unknown directive: {args.mode}")


if __name__ == "__main__":
    cli()
