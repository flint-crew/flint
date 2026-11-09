"""Operations around preserving files and products from an flint run"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from argparse import ArgumentParser
from collections.abc import Collection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from capn_crunch import BaseOptions, add_options_to_parser, create_options_from_parser

from flint.configuration import get_options_from_strategy, load_strategy_yaml
from flint.exceptions import TarArchiveError
from flint.logging import logger
from flint.options import ArchiveDestination, ArchiveOptions, exclude_fields_cli_class
from flint.sclient import run_singularity_command


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


def matching_files(root: Path, patterns: Collection[str]) -> list[Path]:
    """Find the files under a folder that a pattern matches.

    Each pattern is matched whole against the file's path relative to ``root``, in
    POSIX form. A pattern that matches nothing is logged as a warning, as it is
    often a typo.

    Args:
        root (Path): The folder to walk
        patterns (Collection[str]): Regular expressions, any of which may match

    Returns:
        list[Path]: The matching files, relative to ``root``, in sorted order
    """
    root = Path(root)
    compiled = [re.compile(pattern) for pattern in patterns]
    files = []
    unmatched = set(compiled)
    for folder, _, names in os.walk(root):
        for name in names:
            relative = (Path(folder) / name).relative_to(root).as_posix()
            matches = [pattern for pattern in compiled if pattern.fullmatch(relative)]
            if matches:
                files.append(Path(relative))
                unmatched.difference_update(matches)

    for pattern in unmatched:
        logger.warning(f"No file under {root} matches {pattern.pattern!r}")

    return sorted(files)


def rclone_copy(
    container: Path,
    config: Path | None,
    root: Path,
    files: Collection[Path],
    location: str,
) -> None:
    """Copy files with rclone, run from its container.

    rclone checks the hash of every copy, compares rather than copies a file
    already there, and treats one that differs as an error rather than
    overwriting it. Every path is resolved, as the container binds are.

    Args:
        container (Path): The rclone container image
        config (Path | None): The rclone config file, needed for a remote
        root (Path): The folder the files are relative to
        files (Collection[Path]): The files to copy, relative to ``root``
        location (str): An absolute folder path, or ``remote:bucket/folder``

    Raises:
        CalledProcessError: rclone failed
    """
    root = Path(root).resolve()
    config = Path(config).resolve() if config else None
    local = Path(location).resolve() if Path(location).is_absolute() else None
    if local:
        local.mkdir(parents=True, exist_ok=True)
        location = str(local)
    config_flags = ["--config", str(config)] if config else []

    with TemporaryDirectory() as work_dir:
        work = Path(work_dir).resolve()
        listing = work / "files"
        listing.write_text("".join(f"{file}\n" for file in files))
        # The list is passed as the command's own arguments, not a shell line,
        # so paths with spaces survive; --files-from-raw reads names literally
        run_singularity_command(
            image=container,
            command=[
                "rclone",
                *config_flags,
                "copy",
                "--files-from-raw",
                str(listing),
                "--checksum",
                "--immutable",
                str(root),
                location,
            ],
            bind_dirs=[work, *([local] if local else [])],
            read_only_bind_dirs=[root, *([config] if config else [])],
        )


def write_tarball(root: Path, files: Collection[Path], tar_path: Path) -> Path:
    """Write files to one tarball, with its MD5 beside it.

    Members are named ``<tarball stem>/<file>``. The tarball is written under a
    ``.partial`` name and renamed once complete, so one already at ``tar_path``
    is finished and is kept as it is.

    Args:
        root (Path): The folder the files are relative to
        files (Collection[Path]): The files to add, relative to ``root``
        tar_path (Path): The tarball to write

    Returns:
        Path: ``tar_path``
    """
    tar_path = Path(tar_path)
    if tar_path.exists():
        logger.info(f"{tar_path} exists already and is kept")
        return tar_path

    tar_path.parent.mkdir(parents=True, exist_ok=True)
    partial = tar_path.with_name(f"{tar_path.name}.partial")
    logger.info(f"Writing {len(files)} files to {tar_path}")
    # tarfile copies members in 16 KiB pieces by default, too small for lustre
    with tarfile.TarFile(partial, "w", copybufsize=16 * 1024 * 1024) as tar:
        for file in files:
            tar.add(Path(root) / file, arcname=f"{tar_path.stem}/{file}")
    with open(partial, "rb") as handle:
        md5 = hashlib.file_digest(handle, "md5").hexdigest()
    tar_path.with_name(f"{tar_path.name}.md5").write_text(f"{md5}  {tar_path.name}\n")
    os.replace(partial, tar_path)

    return tar_path


def archive_to(
    root: Path,
    field: str,
    destination: ArchiveDestination,
    rclone_container: Path | None = None,
    rclone_config: Path | None = None,
) -> None:
    """Send the files of a field folder that a destination's patterns match to it.

    Args:
        root (Path): The field folder
        field (str): The field's name at the destination, such as ``SB60001``
        destination (ArchiveDestination): Where the files go, and which they are
        rclone_container (Path | None, optional): The rclone container, needed by
            a ``copy`` destination. Defaults to None.
        rclone_config (Path | None, optional): The rclone config, needed by a
            remote. Defaults to None.

    Raises:
        FileNotFoundError: ``root`` is not a folder
        ValueError: No file matches, or a copy destination has no rclone container
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"The field folder {root} does not exist")
    files = matching_files(root=root, patterns=destination.patterns)
    if not files:
        raise ValueError(f"No file under {root} matches a pattern of {destination}")

    target = destination.target(field)
    if destination.method == "tar":
        write_tarball(root=root, files=files, tar_path=Path(target))
        return

    if rclone_container is None:
        raise ValueError(f"Copying to {target} needs the rclone container")
    rclone_copy(
        container=rclone_container,
        config=rclone_config,
        root=root,
        files=files,
        location=target,
    )


def _writable(folder: Path) -> bool:
    """Whether a folder, or the nearest parent that exists, can be written to"""
    while not folder.exists():
        folder = folder.parent
    return os.access(folder, os.W_OK)


def check_archive_prerequisites(
    destinations: Collection[ArchiveDestination],
    rclone_container: Path | None,
    rclone_config: Path | None,
) -> None:
    """Check what the destinations need, so a run fails before its imaging rather
    than after.

    Args:
        destinations (Collection[ArchiveDestination]): Every destination of the run
        rclone_container (Path | None): The rclone container, needed by ``copy``
        rclone_config (Path | None): The rclone config, needed by a remote

    Raises:
        ValueError: Two destinations share a target, a local location cannot be
            written to, or the rclone container or config is unset or missing
    """
    targets = [d.target("<field>") for d in destinations]
    for target in sorted(set(targets)):
        if targets.count(target) > 1:
            raise ValueError(
                f"{targets.count(target)} destinations would all write {target}. "
                "Give each its own location or suffix"
            )

    for destination in destinations:
        if not destination.remote and not _writable(Path(destination.location)):
            raise ValueError(f"{destination.location} cannot be written to")

    copies = [d for d in destinations if d.method == "copy"]
    if copies and not (rclone_container and rclone_container.exists()):
        raise ValueError(
            f"Copying to {copies[0].location} needs an rclone container, "
            f"not {rclone_container}"
        )
    remotes = [d for d in copies if d.remote]
    if remotes and not (rclone_config and rclone_config.exists()):
        raise ValueError(
            f"The remote {remotes[0].location} needs an rclone config, not {rclone_config}"
        )


def archive_options_from_strategy(strategy_yaml_path: Path) -> ArchiveOptions:
    """Read the archive options from a strategy file's ``defaults.archive``.

    Only the defaults are read, so a strategy with no self-calibration section,
    such as one written for ``flint_flow_archive`` alone, serves as well.

    Args:
        strategy_yaml_path (Path): The strategy file

    Returns:
        ArchiveOptions: Its archive options, with none set where it has no section
    """
    strategy = load_strategy_yaml(input_yaml=strategy_yaml_path)
    return ArchiveOptions(**strategy["defaults"].get("archive", {}))


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


def cli_options_class() -> type[BaseOptions]:
    """``ArchiveOptions`` without the destinations, which no flag can express"""
    return exclude_fields_cli_class(ArchiveOptions, {"destinations"})


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
        parser=list_parser, options_class=cli_options_class()
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
        parser=create_parser, options_class=cli_options_class()
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
        parser=copy_parser, options_class=cli_options_class()
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
        archive_options = (
            ArchiveOptions(
                **get_archive_options_from_yaml(
                    strategy_yaml_path=args.strategy_yaml_path
                )
            )
            if args.strategy_yaml_path
            else ArchiveOptions(
                **create_options_from_parser(
                    parser_namespace=args, options_class=cli_options_class()
                )._asdict()
            )
        )

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
        archive_options = (
            ArchiveOptions(
                **get_archive_options_from_yaml(
                    strategy_yaml_path=args.strategy_yaml_path
                )
            )
            if args.strategy_yaml_path
            else ArchiveOptions(
                **create_options_from_parser(
                    parser_namespace=args, options_class=cli_options_class()
                )._asdict()
            )
        )

        create_sbid_tar_archive(
            tar_out_path=args.tar_out_path,
            base_path=args.base_path,
            archive_options=archive_options,
        )
    elif args.mode == "copy":
        archive_options = (
            ArchiveOptions(
                **get_archive_options_from_yaml(
                    strategy_yaml_path=args.strategy_yaml_path
                )
            )
            if args.strategy_yaml_path
            else ArchiveOptions(
                **create_options_from_parser(
                    parser_namespace=args, options_class=cli_options_class()
                )._asdict()
            )
        )
        copy_sbid_files_archive(
            copy_out_path=args.copy_out_path,
            base_path=args.base_path,
            archive_options=archive_options,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
