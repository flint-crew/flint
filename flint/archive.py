"""Operations around preserving files and products from an flint run"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tarfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Collection

from flint.configuration import get_options_from_strategy
from flint.exceptions import TarArchiveError
from flint.logging import logger
from flint.options import (
    ArchiveOptions,
    add_options_to_parser,
    create_options_from_parser,
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
        parser=list_parser, options_class=ArchiveOptions
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
        parser=create_parser, options_class=ArchiveOptions
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
        parser=copy_parser, options_class=ArchiveOptions
    )
    copy_parser.add_argument(
        "--strategy-yaml-path",
        type=Path,
        default=None,
        help="Path to a strategy file with a archive section. Overrides any --file-patterns. ",
    )

    return parser


def cli() -> None:
    parser = get_parser()

    args = parser.parse_args()

    if args.mode == "list":
        archive_options = (
            ArchiveOptions(
                **get_archive_options_from_yaml(
                    strategy_yaml_path=args.strategy_yaml_path
                )
            )
            if args.strategy_yaml_path
            else create_options_from_parser(
                parser_namespace=args, options_class=ArchiveOptions
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
            else create_options_from_parser(
                parser_namespace=args, options_class=ArchiveOptions
            )
        )

        update_options_create: dict[str, Any] = (
            get_archive_options_from_yaml(strategy_yaml_path=args.strategy_yaml_path)
            if args.strategy_yaml_path
            else dict(tar_file_re_patterhs=args.file_patterns)
        )
        archive_options = ArchiveOptions(**update_options_create)

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
            else create_options_from_parser(
                parser_namespace=args, options_class=ArchiveOptions
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
