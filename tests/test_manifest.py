"""Tests around the archive manifest"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flint.manifest import (
    ArchiveDestination,
    ArchiveManifest,
    ArchiveProduct,
    add_destinations,
    destination_products,
    draft_manifest,
    marker_matches,
    read_manifest,
    seal_product,
    sweep_products,
    write_manifest,
)
from flint.options import ArchiveOptions, ProductKind

# Names as RACS-all writes them, with the kind the default patterns should give
RACS_ALL_FILES = {
    "SB60001.RACS_1237+00.round2.i.round2.linmos.fits": "image",
    "SB60001.RACS_1237+00.round2.i.round2.residual.linmos.fits": "image",
    "SB60001.RACS_1237+00.noselfcal.linmos.fits": "image",
    "SB60001.RACS_1237+00.round2.i.round2.weight.fits": "weight",
    "SB60001.RACS_1237+00.round2.i.cube.round2.linmos.fits": "cube",
    "SB60001.RACS_1237+00.round2.i.cube.round2.weight.fits": "weight",
    "SB60001.RACS_1237+00.round2.i.round2.linmos_comp.fits": "catalogue",
    "SB60001.RACS_1237+00.round2.i.round2.linmos_rms.fits": "noise",
    "SB60001.RACS_1237+00.round2.i.round2.linmos_bkg.fits": "noise",
    "validation_plot.png": "report",
    "SB60001.field_summary.csv": "report",
    "racs_all_options-20261107-031400.yaml": "config",
    "SB60001.RACS_1237+00.beam00.round1.caltable.ch0000-0288.tar": "calibration",
    "SB60001.RACS_1237+00.beam00.round1.ms.flagversions.tar": "calibration",
}
# Intermediate products that are not archived
RACS_ALL_SKIPPED = (
    "SB60001.RACS_1237+00.beam00.round2.i.MFS.image.fits",
    "SB60001.RACS_1237+00.beam00.round2.i.cube.fits",
    "SB60001.RACS_1237+00.beam00.round1.ms.tar",
    "SB60001.RACS_1237+00.round2.parset.txt",
)


def _touch(path: Path, text: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_default_patterns_classify_racs_all_names(tmp_path: Path) -> None:
    """Cubes in particular must never be taken for images, which go to the hot copy"""
    for name in (*RACS_ALL_FILES, *RACS_ALL_SKIPPED):
        _touch(tmp_path / name)

    products = sweep_products(root=tmp_path, patterns=ArchiveOptions().sweep_patterns)

    assert products == {Path(name): kind for name, kind in RACS_ALL_FILES.items()}


def test_sweep_enters_plain_folders_only(tmp_path: Path) -> None:
    """Measurement sets and zarr stores are folders full of files that are not products"""
    patterns: dict[str, ProductKind] = {r".*\.png": "report"}
    _touch(tmp_path / "rmsynth" / "fdf.png")
    _touch(tmp_path / "SB60001.beam00.round2.ms" / "plot.png")
    _touch(tmp_path / "q.zarr" / "plot.png")

    products = sweep_products(root=tmp_path, patterns=patterns)

    assert products == {Path("rmsynth/fdf.png"): "report"}


def test_stage_kinds_win_over_the_sweep(tmp_path: Path) -> None:
    cube = _touch(tmp_path / "spice" / "q.linmos.fits.fz")
    image = _touch(tmp_path / "i.linmos.fits")

    manifest = draft_manifest(
        root=tmp_path,
        field="SB60001",
        sbids={"low": 60001},
        products={cube: "cube", image: "catalogue"},
        destinations=(),
        sweep_patterns={r".*\.linmos\.fits": "image"},
    )

    assert manifest.products == (
        ArchiveProduct(path=Path("i.linmos.fits"), kind="catalogue"),
        ArchiveProduct(path=Path("spice/q.linmos.fits.fz"), kind="cube"),
    )


def test_draft_refuses_products_outside_the_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the archive root"):
        draft_manifest(
            root=tmp_path / "root",
            field="SB60001",
            sbids={},
            products={tmp_path / "elsewhere" / "q.fits": "cube"},
            destinations=(),
            sweep_patterns={},
        )


def test_manifest_round_trips(tmp_path: Path) -> None:
    manifest = ArchiveManifest(
        field="SB60001",
        sbids={"low": 60001},
        root=tmp_path,
        sealed=datetime(2026, 11, 7, tzinfo=UTC),
        products=(ArchiveProduct(path=Path("a.fits"), kind="image", size=4, md5="x"),),
        destinations=(
            ArchiveDestination(name="tar", location="/tape", kinds=("image",)),
        ),
    )
    path = write_manifest(manifest=manifest, path=tmp_path / "archive_manifest.json")

    assert read_manifest(path) == manifest
    assert not list(tmp_path.glob("*.partial"))


def test_seal_records_size_and_md5(tmp_path: Path) -> None:
    _touch(tmp_path / "a.fits", "some bytes")

    sealed = seal_product(
        root=tmp_path, product=ArchiveProduct(path=Path("a.fits"), kind="image")
    )

    assert sealed.size == len("some bytes")
    assert sealed.md5 == hashlib.md5(b"some bytes").hexdigest()


def test_seal_refuses_a_missing_product_or_a_folder(tmp_path: Path) -> None:
    (tmp_path / "folder.fits").mkdir()

    for name in ("missing.fits", "folder.fits"):
        with pytest.raises(FileNotFoundError):
            seal_product(
                root=tmp_path, product=ArchiveProduct(path=Path(name), kind="image")
            )


def test_seal_zips_a_measurement_set_first(tmp_path: Path) -> None:
    """A final-round measurement set is listed as the archive it is zipped into"""
    _touch(tmp_path / "SB60001.beam00.round2.ms" / "table.f0")

    sealed = seal_product(
        root=tmp_path,
        product=ArchiveProduct(
            path=Path("SB60001.beam00.round2.ms.tar"), kind="visibilities"
        ),
    )

    assert not (tmp_path / "SB60001.beam00.round2.ms").exists()
    assert sealed.size == (tmp_path / "SB60001.beam00.round2.ms.tar").stat().st_size


def _sealed(tmp_path: Path, **kwargs) -> ArchiveManifest:
    return ArchiveManifest(
        field="SB60001",
        sbids={},
        root=tmp_path,
        sealed=datetime(2026, 11, 7, tzinfo=UTC),
        products=(
            ArchiveProduct(path=Path("i.fits"), kind="image", size=1, md5="a"),
            ArchiveProduct(path=Path("q.fits"), kind="cube", size=2, md5="b"),
        ),
        **kwargs,
    )


def test_marker_compares_only_the_destination_products(tmp_path: Path) -> None:
    manifest = _sealed(tmp_path)
    images = ArchiveDestination(name="copy", location="/hot", kinds=("image",))
    marker = manifest.with_options(
        products=(
            manifest.products[0],
            manifest.products[1].with_options(md5="changed"),
        )
    )

    assert destination_products(manifest, images) == (manifest.products[0],)
    assert marker_matches(marker=marker, manifest=manifest, destination=images)
    assert not marker_matches(
        marker=marker,
        manifest=manifest,
        destination=images.with_options(kinds=("image", "cube")),
    )
    assert not marker_matches(marker=None, manifest=manifest, destination=images)
    assert not marker_matches(
        marker=marker.with_options(sealed=datetime.now(UTC)),
        manifest=manifest,
        destination=images,
    )


def test_adding_a_destination_keeps_the_other_markers(tmp_path: Path) -> None:
    tar = ArchiveDestination(name="tar", location="/tape", kinds=("image", "cube"))
    remote = ArchiveDestination(name="acacia", location="acacia:a", kinds=("image",))
    marker = _sealed(tmp_path, destinations=(tar,))

    manifest = add_destinations(manifest=marker, destinations=(tar, remote))

    assert manifest.destinations == (tar, remote)
    assert marker_matches(marker=marker, manifest=manifest, destination=tar)


def test_a_destination_cannot_change_once_listed(tmp_path: Path) -> None:
    tar = ArchiveDestination(name="tar", location="/tape", kinds=("image",))
    manifest = _sealed(tmp_path, destinations=(tar,))

    with pytest.raises(ValueError, match="listed already"):
        add_destinations(
            manifest=manifest, destinations=(tar.with_options(location="/other"),)
        )
