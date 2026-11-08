"""Copying archived products with rclone, run from its container"""

from __future__ import annotations

import configparser
from collections.abc import Collection
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory

from flint.logging import logger
from flint.manifest import (
    MANIFEST_NAME,
    ArchiveManifest,
    ArchiveProduct,
    read_manifest,
    write_manifest,
)
from flint.sclient import run_singularity_command


def config_remotes(config: Path) -> set[str]:
    """The names of the remotes an rclone config file defines"""
    parser = configparser.ConfigParser()
    parser.read(config)
    return set(parser.sections())


def _run_rclone(
    container: Path,
    config: Path | None,
    arguments: str,
    bind_dirs: Collection[Path] = (),
    read_only_bind_dirs: Collection[Path] = (),
) -> None:
    config_flag = f"--config {config} " if config else ""
    run_singularity_command(
        image=container,
        command=f"rclone {config_flag}{arguments}",
        bind_dirs=bind_dirs,
        read_only_bind_dirs=[*read_only_bind_dirs, *([config] if config else [])],
    )


def rclone_copy(
    container: Path,
    config: Path | None,
    root: Path,
    products: Collection[ArchiveProduct],
    location: str,
) -> None:
    """Copy products from ``root`` to ``location``, then check each arrived intact.

    A file already there is compared by size and MD5, and one that differs is an
    error rather than overwritten. A local ``location`` is a folder path.

    Raises:
        CalledProcessError: A copy failed, or a file at ``location`` does not match
    """
    local = Path(location) if location.startswith("/") else None
    if local:
        local.mkdir(parents=True, exist_ok=True)

    logger.info(f"Copying {len(products)} products from {root} to {location}")
    with TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        files = work / "files"
        files.write_text("".join(f"{product.path}\n" for product in products))
        sums = work / "md5"
        sums.write_text(
            "".join(f"{product.md5}  {product.path}\n" for product in products)
        )

        binds = [work, *([local] if local else [])]
        _run_rclone(
            container=container,
            config=config,
            arguments=f"copy --files-from {files} --checksum --immutable {root} {location}",
            bind_dirs=binds,
            read_only_bind_dirs=[root],
        )
        # The destination's own hashes are compared, so the source is not read again
        _run_rclone(
            container=container,
            config=config,
            arguments=f"checksum md5 --one-way {sums} {location}",
            bind_dirs=binds,
        )


def rclone_read_marker(
    container: Path, config: Path | None, location: str
) -> ArchiveManifest | None:
    """The marker manifest at ``location``, or None when it cannot be read"""
    with TemporaryDirectory() as work_dir:
        marker = Path(work_dir) / MANIFEST_NAME
        try:
            _run_rclone(
                container=container,
                config=config,
                arguments=f"copyto {location}/{MANIFEST_NAME} {marker}",
                bind_dirs=[Path(work_dir)],
            )
        except CalledProcessError:
            logger.info(f"No marker could be read from {location}")
            return None

        return read_manifest(marker) if marker.exists() else None


def rclone_write_marker(
    container: Path, config: Path | None, manifest: ArchiveManifest, location: str
) -> None:
    """Write the marker manifest to ``location``"""
    with TemporaryDirectory() as work_dir:
        marker = write_manifest(manifest=manifest, path=Path(work_dir) / MANIFEST_NAME)
        _run_rclone(
            container=container,
            config=config,
            arguments=f"copyto {marker} {location}/{MANIFEST_NAME}",
            bind_dirs=[Path(work_dir)],
        )
