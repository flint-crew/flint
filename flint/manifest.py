"""The list of products a field run archives, and the checks made against it.

A manifest is drafted from what the stages produced, sealed once on the data mover
with the size and MD5 of every product, and copied to each destination last, where
it marks that destination as complete.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path

from capn_crunch import BaseOptions
from pydantic import Field

from flint import __version__
from flint.options import ProductKind
from flint.utils import zip_folder

MANIFEST_NAME = "archive_manifest.json"
"""Name of the manifest in the processing folder and at each copy destination"""


class ArchiveProduct(BaseOptions):
    """One file the archive keeps"""

    path: Path
    """Relative to the manifest root"""
    kind: ProductKind
    """What it is, used by destinations to pick products"""
    size: int | None = None
    """Bytes, set when the manifest is sealed"""
    md5: str | None = None
    """Set when the manifest is sealed"""


class ArchiveDestination(BaseOptions):
    """One place a field is copied to"""

    name: str
    """``copy``, ``tar``, or the name of a remote"""
    location: str
    """Folder, tar folder, or rclone remote and bucket"""
    kinds: tuple[ProductKind, ...]
    """The kinds of product it takes"""


class ArchiveManifest(BaseOptions):
    """Every product of one field run, and every destination it goes to"""

    version: int = 1
    field: str
    """Folder name used at every destination"""
    sbids: dict[str, int]
    """SBID per band"""
    root: Path
    """Processing folder the product paths are relative to"""
    flint_version: str = __version__
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sealed: datetime | None = None
    """Set once every product has a size and MD5"""
    products: tuple[ArchiveProduct, ...] = ()
    destinations: tuple[ArchiveDestination, ...] = ()
    """Every destination the purge waits for"""


def field_name(low_sbid: int | str) -> str:
    """The folder name a field takes at every destination"""
    return f"SB{low_sbid}"


def sweep_products(
    root: Path, patterns: Mapping[str, ProductKind]
) -> dict[Path, ProductKind]:
    """The files under ``root`` whose names a pattern matches, by path relative to it.

    Folders with a suffix, such as measurement sets and zarr stores, are not entered:
    they are not products, and some hold thousands of files.
    """
    compiled = [(re.compile(pattern), kind) for pattern, kind in patterns.items()]
    products: dict[Path, ProductKind] = {}
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = [name for name in subfolders if not Path(name).suffix]
        for name in files:
            kind = next(
                (kind for pattern, kind in compiled if pattern.fullmatch(name)), None
            )
            if kind is not None:
                products[(Path(folder) / name).relative_to(root)] = kind

    return products


def draft_manifest(
    root: Path,
    field: str,
    sbids: dict[str, int],
    products: Mapping[Path, ProductKind],
    destinations: Collection[ArchiveDestination],
    sweep_patterns: Mapping[str, ProductKind],
) -> ArchiveManifest:
    """List the swept files and the stage products, without reading any of them.

    A product a stage handed over keeps the kind the stage gave it.

    Raises:
        ValueError: A product lies outside ``root``
    """
    root = Path(root).absolute()
    listed = sweep_products(root=root, patterns=sweep_patterns)
    for path, kind in products.items():
        path = Path(path).absolute()
        if not path.is_relative_to(root):
            raise ValueError(f"{path} is outside the archive root {root}")
        listed[path.relative_to(root)] = kind

    return ArchiveManifest(
        field=field,
        sbids=sbids,
        root=root,
        products=tuple(
            ArchiveProduct(path=path, kind=kind)
            for path, kind in sorted(listed.items())
        ),
        destinations=tuple(destinations),
    )


def read_manifest(path: Path) -> ArchiveManifest:
    """Read a manifest written by ``write_manifest``"""
    return ArchiveManifest.model_validate_json(Path(path).read_text())


def write_manifest(manifest: ArchiveManifest, path: Path) -> Path:
    """Write a manifest, replacing an earlier one only once the new one is complete"""
    path = Path(path)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(manifest.model_dump_json(indent=2))
    os.replace(partial, path)

    return path


def md5_file(path: Path) -> str:
    """The MD5 of a file"""
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "md5").hexdigest()


def seal_product(root: Path, product: ArchiveProduct) -> ArchiveProduct:
    """Record a product's size and MD5, first zipping the measurement set it names.

    A zip left unfinished by an earlier run is finished here, as the folder is only
    removed once its archive is complete.

    Raises:
        FileNotFoundError: The product is not a regular file
    """
    path = Path(root) / product.path
    folder = path.with_suffix("")
    if product.kind == "visibilities" and folder.is_dir():
        zip_folder(in_path=folder)

    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file, so cannot be archived")

    return product.with_options(size=path.stat().st_size, md5=md5_file(path))


def destination_products(
    manifest: ArchiveManifest, destination: ArchiveDestination
) -> tuple[ArchiveProduct, ...]:
    """The products a destination takes"""
    return tuple(
        product for product in manifest.products if product.kind in destination.kinds
    )


def marker_matches(
    marker: ArchiveManifest | None,
    manifest: ArchiveManifest,
    destination: ArchiveDestination,
) -> bool:
    """Whether a destination's marker shows it holds the products of this manifest.

    Only that destination's products are compared, so adding another destination to
    the manifest leaves the existing markers valid.
    """
    return (
        marker is not None
        and manifest.sealed is not None
        and marker.sealed == manifest.sealed
        and destination_products(marker, destination)
        == destination_products(manifest, destination)
    )


def add_destinations(
    manifest: ArchiveManifest, destinations: Collection[ArchiveDestination]
) -> ArchiveManifest:
    """Add the destinations a manifest does not list yet.

    Raises:
        ValueError: A destination is listed already, with another location or kinds
    """
    listed = {destination.name: destination for destination in manifest.destinations}
    for destination in destinations:
        existing = listed.setdefault(destination.name, destination)
        if existing != destination:
            raise ValueError(
                f"{destination.name} is listed already as {existing}, not {destination}"
            )

    return manifest.with_options(destinations=tuple(listed.values()))
