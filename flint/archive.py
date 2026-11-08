"""Operations around preserving files and products from an flint run"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from argparse import ArgumentParser, Namespace
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, BinaryIO, get_args

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser

from flint.configuration import get_options_from_strategy
from flint.exceptions import TarArchiveError
from flint.logging import logger
from flint.manifest import (
    MANIFEST_NAME,
    ArchiveDestination,
    ArchiveManifest,
    destination_products,
    marker_matches,
    read_manifest,
    write_manifest,
)
from flint.options import (
    ArchiveOptions,
    ArchiveRemote,
    ProductKind,
    exclude_fields_cli_class,
)
from flint.rclone import (
    config_remotes,
    rclone_copy,
    rclone_read_marker,
    rclone_write_marker,
)


def resolve_glob_expressions(
    base_path: Path, file_re_patterns: Collection[str]
) -> tuple[Path, ...]:
    """Collect a set of files given a base directory and a set of glob expressions. Unique
    paths are returned.

    Args:
        base_path (Path): The base folder with files to consider
        file_re_patterns (Collection[str]): An iterable with a set of regular-expression patterns to evaluate

    Returns:
        Tuple[Path,...]: Unique collection of paths
    """
    base_path = Path(base_path)

    resolved_files: list[Path] = []

    logger.info(f"Searching {base_path=}")

    all_files = list(base_path.iterdir())
    logger.info(f"{len(all_files)} total files and {len(file_re_patterns)} to consider")

    for reg_expression in file_re_patterns:
        logger.info(f"Using expression: {reg_expression}")
        resolved_files.extend(
            [f for f in all_files if re.search(reg_expression, str(f.name))]
        )

    logger.info(
        f"Resolved {len(resolved_files)} files from {len(file_re_patterns)} expressions in {base_path=}"
    )

    return tuple(sorted([Path(p) for p in set(resolved_files)]))


def copy_files_into(copy_out_path: Path, files_to_copy: Collection[Path]) -> Path:
    """Copy a set of specified files into an output directory, If a file happens to
    be a folder then it will be copied over.

    Args:
        copy_out_path (Path): Path to copy files into
        files_to_copy (Collection[Path]): Files that shall be copied

    Returns:
        Path: The path files were copied into
    """

    copy_out_path = Path(copy_out_path)

    copy_out_path.mkdir(parents=True, exist_ok=True)
    total = len(files_to_copy)
    not_copied: list[Path] = []

    logger.info(f"Copying {total} files into {copy_out_path}")
    for count, file in enumerate(files_to_copy):
        if file.is_file():
            logger.info(f"{count + 1} of {total}, copying file {file}")
            shutil.copy(file, copy_out_path)
        elif file.is_dir():
            # TODO: Add an option to tar folders into the final location
            logger.info(f"{count + 1} of {total}, copying folder {file}")
            shutil.copytree(file, copy_out_path / file.name)
        else:
            not_copied.append(file)
            logger.critical(f"{file} is not a file. Skipping. ")
            continue

    if not_copied:
        logger.critical(f"Did not copy {len(not_copied)} files, {not_copied=}")

    return copy_out_path


def verify_tarball(
    tarball: Path,
) -> bool:
    """Verify that a tarball was created properly by examining its
    table. Internally this calls ``tar`` through a subprocess call.
    Hence, ``tar`` needs to be available on the system PATH.

    Args:
        tarball (Path): The tarball to examine

    Returns:
        bool: True if the ``tar`` exit code is 0, False otherwise
    """
    tarball = Path(tarball)  # trust nothing
    assert tarball.exists() and tarball.is_file(), (
        f"{tarball} is not a file or does not exist"
    )
    assert tarball.suffix == ".tar", f"{tarball=} appears to not have a .tar extension"

    cmd = f"tar -tvf {tarball!s}"
    logger.info(f"Verifying {tarball=}")
    popen = subprocess.Popen(shlex.split(cmd), stderr=subprocess.PIPE)
    with popen.stderr:  # type: ignore
        for line in iter(popen.stderr.readline, b""):  # type: ignore
            logger.error(line.decode().strip())
    exitcode = popen.wait()

    return exitcode == 0


# TODO: Add a clobber option
def tar_files_into(
    tar_out_path: Path, files_to_tar: Collection[Path], verify: bool = True
) -> Path:
    """Create a tar file given a desired output path and list of files to tar.

    Args:
        tar_out_path (Path): The output path of the tarball. The parent directory will be created if necessary.
        files_to_tar (Collection[Path]): All the files to tarball up
        verify (bool, optional): Verify that the tarball was correctly formed. Defaults to True.

    Raises:
        FileExistsError: The path of the tarball created

    Returns:
        Path: There exists a tarball of the same name
    """

    tar_out_path = Path(tar_out_path)

    if tar_out_path.exists():
        raise FileExistsError(f"{tar_out_path} already exists. ")

    # Create the output directory in case it does not exist
    tar_out_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(files_to_tar)
    logger.info(f"Taring {total} files")
    logger.info(f"Opening {tar_out_path}")
    with tarfile.open(tar_out_path, "w") as tar:
        for count, file in enumerate(files_to_tar):
            logger.info(f"{count + 1} of {total}, adding {file!s}")
            tar.add(file, arcname=file.name)

    logger.info(f"Created {tar_out_path}")

    if verify:
        tar_success = verify_tarball(tarball=tar_out_path)
        if not tar_success:
            raise TarArchiveError(f"Failed to verify {tar_out_path=}")

        logger.info(f"{tar_out_path=} appears to be correctly formed")

    return tar_out_path


def create_sbid_tar_archive(
    tar_out_path: Path, base_path: Path, archive_options: ArchiveOptions
) -> Path:
    """Create a tar file of key products in a SBID folder.

    Args:
        tar_out_path (Path): The output location of the tarball to write
        base_path (Path): The base directory that contains files to archive
        archive_options (ArchiveOptions): Options relating to how files are found and archived

    Returns:
        Path: Output tarball directory
    """

    files_to_tar = resolve_glob_expressions(
        base_path=base_path, file_re_patterns=archive_options.tar_file_re_patterns
    )

    tar_out_path = tar_files_into(tar_out_path=tar_out_path, files_to_tar=files_to_tar)

    return tar_out_path


def copy_sbid_files_archive(
    copy_out_path: Path, base_path: Path, archive_options: ArchiveOptions
) -> Path:
    """Copy files from an SBID processing folder into a final location. Uses the
    `copy_file_globs` set of expressions to identify files to copy.

    Args:
        copy_out_path (Path): The output location of the tarball to write
        base_path (Path): The base directory that contains files to archive
        archive_options (ArchiveOptions): Options relating to how files are found and archived

    Returns:
        Path: Output tarball directory
    """

    files_to_copy = resolve_glob_expressions(
        base_path=base_path, file_re_patterns=archive_options.copy_file_re_patterns
    )

    copy_out_path = copy_files_into(
        copy_out_path=copy_out_path, files_to_copy=files_to_copy
    )

    return copy_out_path


def get_archive_options_from_yaml(strategy_yaml_path: Path) -> dict[str, Any]:
    """Load the archive options from a specified strategy file

    Args:
        strategy_yaml_path (Path): The path to the strategy yaml file containing archive options

    Returns:
        Dict[str, Any]: Loaded options for ArchiveOptions
    """
    archive_options = get_options_from_strategy(
        strategy=strategy_yaml_path, mode="archive", round_info=0, operation="selfcal"
    )

    logger.info(f"{archive_options=}")

    return archive_options


def archive_destinations(
    copy_path: Path | None,
    tar_path: Path | None,
    remote_names: Collection[str],
    remotes: Mapping[str, ArchiveRemote],
) -> tuple[ArchiveDestination, ...]:
    """The destinations a run enables, each with the kinds of product it takes.

    Raises:
        ValueError: A remote is not defined, or takes a name kept for the copy or tar
    """
    missing = set(remote_names) - set(remotes)
    if missing:
        raise ValueError(f"No archive remote is defined for {sorted(missing)}")
    reserved = {"copy", "tar"} & set(remote_names)
    if reserved:
        raise ValueError(f"{sorted(reserved)} are kept for the hot copy and tarball")

    destinations = []
    if copy_path:
        destinations.append(
            ArchiveDestination(
                name="copy",
                location=str(Path(copy_path).absolute()),
                kinds=("image", "catalogue", "report"),
            )
        )
    if tar_path:
        destinations.append(
            ArchiveDestination(
                name="tar",
                location=str(Path(tar_path).absolute()),
                kinds=get_args(ProductKind),
            )
        )
    destinations.extend(
        ArchiveDestination(
            name=name, location=remotes[name].remote, kinds=remotes[name].kinds
        )
        for name in remote_names
    )

    return tuple(destinations)


def check_archive_prerequisites(
    destinations: Collection[ArchiveDestination],
    rclone_container: Path | None,
    rclone_config: Path | None,
) -> None:
    """Check what the destinations need, before anything is processed or copied.

    Raises:
        ValueError: A container, config or remote is missing, or a root is not writable
    """
    if any(destination.name != "tar" for destination in destinations):
        if rclone_container is None or not rclone_container.exists():
            raise ValueError(
                f"The archive needs the rclone container, {rclone_container=}"
            )

    remotes = {
        d.location.split(":")[0] for d in destinations if d.name not in ("copy", "tar")
    }
    if remotes:
        if rclone_config is None or not rclone_config.exists():
            raise ValueError(f"Archive remotes need an rclone config, {rclone_config=}")
        missing = remotes - config_remotes(rclone_config)
        if missing:
            raise ValueError(f"{rclone_config} defines no remote {sorted(missing)}")

    for destination in destinations:
        if destination.name in ("copy", "tar") and not os.access(
            destination.location, os.W_OK
        ):
            raise ValueError(f"The archive cannot write to {destination.location}")


class _Md5File:
    """A file that hashes what is read from or written to it"""

    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        self.md5 = hashlib.md5()

    def read(self, size: int = -1) -> bytes:
        data = self.file.read(size)
        self.md5.update(data)
        return data

    def write(self, data: bytes) -> int:
        self.md5.update(data)
        return self.file.write(data)


def tarball_path(manifest: ArchiveManifest, destination: ArchiveDestination) -> Path:
    """Where a field's tarball goes, named by when its manifest was sealed"""
    assert manifest.sealed is not None, "Only a sealed manifest is archived"
    return (
        Path(destination.location)
        / f"{manifest.field}.{manifest.sealed:%Y%m%d-%H%M%S}.tar"
    )


def write_tarball(manifest: ArchiveManifest, destination: ArchiveDestination) -> Path:
    """Write the tarball of a destination's products, checking each one as it goes.

    The tar is written under a temporary name and renamed once it is complete and on
    disk, so a tar under its final name is whole. Its MD5 goes in a ``.md5`` file
    beside it, and nothing is read back, as reading tape-backed storage can trigger
    a recall.

    Raises:
        ValueError: A product no longer has the MD5 the manifest gives it
    """
    tar_path = tarball_path(manifest=manifest, destination=destination)
    if tar_path.exists():
        logger.info(f"{tar_path} is complete already")
        return tar_path

    tar_path.parent.mkdir(parents=True, exist_ok=True)
    partial = tar_path.with_name(f"{tar_path.name}.partial")
    block = 16 * 1024 * 1024
    try:
        with open(partial, "wb") as out_file:
            tar_stream = _Md5File(out_file)
            # A stream only writes, so the hashing file needs no tell or seek
            with tarfile.open(  # type: ignore[call-overload]
                fileobj=tar_stream, mode="w|", bufsize=block
            ) as tar:
                for product in destination_products(manifest, destination):
                    path = manifest.root / product.path
                    logger.info(f"Adding {path} to {tar_path}")
                    info = tar.gettarinfo(
                        path, arcname=f"{manifest.field}/{product.path}"
                    )
                    with open(path, "rb") as in_file:
                        member = _Md5File(in_file)
                        tar.addfile(info, fileobj=member)
                    if member.md5.hexdigest() != product.md5:
                        raise ValueError(
                            f"{path} changed after the manifest was sealed"
                        )

                # Carried inside, so the MD5s travel with the products on tape
                contents = manifest.model_dump_json(indent=2).encode()
                info = tarfile.TarInfo(name=f"{manifest.field}/{MANIFEST_NAME}")
                info.size = len(contents)
                info.mtime = int(manifest.sealed.timestamp()) if manifest.sealed else 0
                tar.addfile(info, fileobj=io.BytesIO(contents))
            out_file.flush()
            os.fsync(out_file.fileno())
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    Path(f"{tar_path}.md5").write_text(
        f"{tar_stream.md5.hexdigest()}  {tar_path.name}\n"
    )
    os.replace(partial, tar_path)
    folder = os.open(tar_path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)

    return tar_path


def read_marker(
    manifest: ArchiveManifest,
    destination: ArchiveDestination,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> ArchiveManifest | None:
    """The marker a destination holds for this field, or None without one"""
    if destination.name == "tar":
        marker = tarball_path(manifest, destination).with_suffix(".manifest.json")
    elif destination.name == "copy":
        marker = Path(destination.location) / manifest.field / MANIFEST_NAME
    else:
        assert rclone_container is not None, "Reading a remote marker needs rclone"
        return rclone_read_marker(
            container=rclone_container,
            config=rclone_config,
            location=f"{destination.location}/{manifest.field}",
        )

    return read_manifest(marker) if marker.exists() else None


def archive_to(
    manifest: ArchiveManifest,
    destination: ArchiveDestination,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> ArchiveDestination:
    """Copy a field to one destination and verify it, writing its marker last.

    A destination whose marker matches the manifest already is left alone.
    """
    marker = read_marker(
        manifest=manifest,
        destination=destination,
        rclone_container=rclone_container,
        rclone_config=rclone_config,
    )
    if marker_matches(marker=marker, manifest=manifest, destination=destination):
        logger.info(f"{destination.name} holds {manifest.field} already")
        return destination

    if destination.name == "tar":
        tar_path = write_tarball(manifest=manifest, destination=destination)
        write_manifest(manifest=manifest, path=tar_path.with_suffix(".manifest.json"))
        return destination

    assert rclone_container is not None, (
        f"{destination.name} needs the rclone container"
    )
    location = f"{destination.location}/{manifest.field}"
    is_copy = destination.name == "copy"
    rclone_copy(
        container=rclone_container,
        config=None if is_copy else rclone_config,
        root=manifest.root,
        products=destination_products(manifest, destination),
        location=location,
    )
    if is_copy:
        write_manifest(manifest=manifest, path=Path(location) / MANIFEST_NAME)
    else:
        rclone_write_marker(
            container=rclone_container,
            config=rclone_config,
            manifest=manifest,
            location=location,
        )

    return destination


def check_archive(
    manifest: ArchiveManifest,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> bool:
    """Whether every destination the manifest lists holds a matching marker.

    A manifest that is not sealed, or lists no destination, has archived nothing.
    """
    complete = manifest.sealed is not None and bool(manifest.destinations)
    for destination in manifest.destinations:
        marker = read_marker(
            manifest=manifest,
            destination=destination,
            rclone_container=rclone_container,
            rclone_config=rclone_config,
        )
        matches = marker_matches(
            marker=marker, manifest=manifest, destination=destination
        )
        logger.info(f"{destination.name}: {'complete' if matches else 'incomplete'}")
        complete = complete and matches

    return complete


def _archive_cli_options() -> type[BaseOptions]:
    """ArchiveOptions without the fields a command line cannot express"""
    return exclude_fields_cli_class(ArchiveOptions, {"remotes", "sweep_patterns"})


def _archive_options_from_args(args: Namespace) -> ArchiveOptions:
    """The archive options from a strategy file if one is given, else the command line"""
    if args.strategy_yaml_path:
        return ArchiveOptions(
            **get_archive_options_from_yaml(strategy_yaml_path=args.strategy_yaml_path)
        )
    cli_options = create_options_from_parser(
        parser_namespace=args, options_class=_archive_cli_options()
    )
    return ArchiveOptions(**cli_options._asdict())


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Operations around archiving. Patterns are specified as regular expressions (not globs). "
    )

    subparser = parser.add_subparsers(
        dest="mode", help="Operation mode of flint_archive"
    )

    list_parser = subparser.add_parser(
        "list", help="List the files that would be copied"
    )
    list_parser.add_argument(
        "--base-path",
        type=Path,
        default=Path("."),
        help="Base directory to perform glob expressions",
    )
    list_parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        default=None,
        help="Path to a strategy file with a archive section. Overrides any --file-patterns. ",
    )
    list_parser.add_argument(
        "--list-mode",
        choices=("create", "copy"),
        default="copy",
        help="Which set of RE patterns to present, those for the tarball (create) or those for copy",
    )
    list_parser = add_options_to_parser(
        parser=list_parser, options_class=_archive_cli_options()
    )
    create_parser = subparser.add_parser("create", help="Create a tarfile archive")
    create_parser.add_argument(
        "tar_out_path", type=Path, help="Path of the output tar file to be created"
    )
    create_parser.add_argument(
        "--base-path",
        type=Path,
        default=Path("."),
        help="Base directory to perform glob expressions",
    )

    create_parser = add_options_to_parser(
        parser=create_parser, options_class=_archive_cli_options()
    )
    create_parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        default=None,
        help="Path to a strategy file with a archive section. Overrides any --file-patterns. ",
    )

    copy_parser = subparser.add_parser(
        "copy", help="Copy a set of files into a output directory"
    )
    copy_parser.add_argument(
        "copy_out_path",
        type=Path,
        help="Path of the output folder that files will be copied into",
    )
    copy_parser.add_argument(
        "--base-path",
        type=Path,
        default=Path("."),
        help="Base directory to perform glob expressions",
    )

    copy_parser = add_options_to_parser(
        parser=copy_parser, options_class=_archive_cli_options()
    )
    copy_parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        default=None,
        help="Path to a strategy file with a archive section. Overrides any --file-patterns. ",
    )

    return parser


def cli(argv: list[str] | None = None) -> None:
    parser = get_parser()

    args = parser.parse_args(argv)

    if args.mode == "list":
        archive_options = _archive_options_from_args(args=args)

        files = resolve_glob_expressions(
            base_path=args.base_path,
            file_re_patterns=(
                archive_options.tar_file_re_patterns
                if args.list_mode == "create"
                else archive_options.copy_file_re_patterns
            ),
        )
        for count, file in enumerate(sorted(files)):
            logger.info(f"{count} of {len(files)}, {file}")
        logger.info(f"{len(files)} for mode={args.list_mode}")

    elif args.mode == "create":
        archive_options = _archive_options_from_args(args=args)

        create_sbid_tar_archive(
            tar_out_path=args.tar_out_path,
            base_path=args.base_path,
            archive_options=archive_options,
        )
    elif args.mode == "copy":
        archive_options = _archive_options_from_args(args=args)
        copy_sbid_files_archive(
            copy_out_path=args.copy_out_path,
            base_path=args.base_path,
            archive_options=archive_options,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
