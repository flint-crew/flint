"""Tests around the archive destinations: hot copy, tarball and remotes"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flint import archive
from flint.archive import (
    archive_destinations,
    archive_to,
    check_archive,
    check_archive_prerequisites,
    tarball_path,
    write_tarball,
)
from flint.manifest import (
    MANIFEST_NAME,
    ArchiveDestination,
    ArchiveManifest,
    ArchiveProduct,
    md5_file,
    read_manifest,
)
from flint.options import ArchiveRemote

SEALED = datetime(2026, 11, 7, 5, 2, 11, tzinfo=UTC)


@pytest.fixture
def manifest(tmp_path: Path) -> ArchiveManifest:
    """A sealed manifest of a small field"""
    root = tmp_path / "60001"
    files = {"i.linmos.fits": "image", "rmsynth/fdf.fits": "rm", "q.cube.fits": "cube"}
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"contents of {name}")

    return ArchiveManifest(
        field="SB60001",
        sbids={"low": 60001},
        root=root,
        sealed=SEALED,
        products=tuple(
            ArchiveProduct(
                path=Path(name),
                kind=kind,
                size=(root / name).stat().st_size,
                md5=md5_file(root / name),
            )
            for name, kind in files.items()
        ),
    )


def _tar(tmp_path: Path) -> ArchiveDestination:
    (tmp_path / "tape").mkdir(exist_ok=True)
    return ArchiveDestination(
        name="tar", location=str(tmp_path / "tape"), kinds=("image", "rm")
    )


def test_tarball_holds_the_products_then_the_manifest(
    tmp_path: Path, manifest: ArchiveManifest
) -> None:
    tar_path = write_tarball(manifest=manifest, destination=_tar(tmp_path))

    assert tar_path == tmp_path / "tape" / "SB60001.20261107-050211.tar"
    with tarfile.open(tar_path) as tar:
        assert tar.getnames() == [
            "SB60001/i.linmos.fits",
            "SB60001/rmsynth/fdf.fits",
            f"SB60001/{MANIFEST_NAME}",
        ]
    md5, name = Path(f"{tar_path}.md5").read_text().split()
    assert (md5, name) == (
        hashlib.md5(tar_path.read_bytes()).hexdigest(),
        tar_path.name,
    )
    assert not list((tmp_path / "tape").glob("*.partial"))


def test_a_product_changed_since_sealing_leaves_no_tarball(
    tmp_path: Path, manifest: ArchiveManifest
) -> None:
    (manifest.root / "rmsynth/fdf.fits").write_text("changed contents here")

    with pytest.raises(ValueError, match="changed after the manifest was sealed"):
        write_tarball(manifest=manifest, destination=_tar(tmp_path))

    assert not list((tmp_path / "tape").iterdir())


def test_a_finished_tarball_is_not_rewritten(
    tmp_path: Path, manifest: ArchiveManifest
) -> None:
    tar_path = tarball_path(manifest=manifest, destination=_tar(tmp_path))
    tar_path.write_text("finished earlier")

    assert write_tarball(manifest=manifest, destination=_tar(tmp_path)) == tar_path
    assert tar_path.read_text() == "finished earlier"


def test_tar_destination_writes_its_marker_and_is_then_skipped(
    tmp_path: Path, manifest: ArchiveManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _tar(tmp_path)
    archive_to(manifest=manifest, destination=destination)

    marker = tmp_path / "tape" / "SB60001.20261107-050211.manifest.json"
    assert read_manifest(marker) == manifest

    monkeypatch.setattr(
        archive, "write_tarball", lambda **kwargs: pytest.fail("rewritten")
    )
    archive_to(manifest=manifest, destination=destination)


def _fake_rclone_copy(container, config, root, products, location) -> None:
    for product in products:
        target = Path(location) / product.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / product.path, target)


def test_hot_copy_takes_its_kinds_and_is_marked(
    tmp_path: Path, manifest: ArchiveManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive, "rclone_copy", _fake_rclone_copy)
    (copy,) = archive_destinations(
        copy_path=tmp_path / "hot", tar_path=None, remote_names=(), remotes={}
    )

    archive_to(manifest=manifest, destination=copy, rclone_container=tmp_path)

    field = tmp_path / "hot" / "SB60001"
    assert sorted(p.name for p in field.rglob("*") if p.is_file()) == [
        MANIFEST_NAME,
        "i.linmos.fits",
    ]
    assert check_archive(manifest=manifest.with_options(destinations=(copy,)))


def test_remote_destination_uses_its_own_marker(
    tmp_path: Path, manifest: ArchiveManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied, marked = [], []
    monkeypatch.setattr(archive, "rclone_read_marker", lambda **kwargs: None)
    monkeypatch.setattr(archive, "rclone_copy", lambda **kwargs: copied.append(kwargs))
    monkeypatch.setattr(
        archive, "rclone_write_marker", lambda **kwargs: marked.append(kwargs)
    )
    (remote,) = archive_destinations(
        copy_path=None,
        tar_path=None,
        remote_names=["acacia"],
        remotes={"acacia": ArchiveRemote(remote="acacia:racs-all", kinds=("cube",))},
    )

    archive_to(
        manifest=manifest,
        destination=remote,
        rclone_container=tmp_path / "rclone.sif",
        rclone_config=tmp_path / "rclone.conf",
    )

    assert copied[0]["location"] == "acacia:racs-all/SB60001"
    assert [p.path for p in copied[0]["products"]] == [Path("q.cube.fits")]
    assert copied[0]["config"] == tmp_path / "rclone.conf"
    assert marked[0]["location"] == "acacia:racs-all/SB60001"


def test_check_archive_needs_every_marker(
    tmp_path: Path, manifest: ArchiveManifest
) -> None:
    destination = _tar(tmp_path)
    listed = manifest.with_options(destinations=(destination,))

    assert not check_archive(manifest=listed)
    archive_to(manifest=listed, destination=destination)
    assert check_archive(manifest=listed)
    assert not check_archive(manifest=manifest)


def test_archive_destinations_refuse_unknown_and_reserved_remotes() -> None:
    with pytest.raises(ValueError, match="No archive remote"):
        archive_destinations(
            copy_path=None, tar_path=None, remote_names=["dap"], remotes={}
        )
    with pytest.raises(ValueError, match="kept for the hot copy"):
        archive_destinations(
            copy_path=None,
            tar_path=None,
            remote_names=["tar"],
            remotes={"tar": ArchiveRemote(remote="a:b")},
        )


def test_tarball_takes_every_kind(tmp_path: Path) -> None:
    (tar,) = archive_destinations(
        copy_path=None, tar_path=tmp_path, remote_names=(), remotes={}
    )
    assert "visibilities" in tar.kinds
    assert "calibration" in tar.kinds


def test_prerequisites(tmp_path: Path) -> None:
    container, config = tmp_path / "rclone.sif", tmp_path / "rclone.conf"
    remote = ArchiveDestination(name="acacia", location="acacia:a", kinds=("cube",))
    copy = ArchiveDestination(name="copy", location=str(tmp_path / "hot"), kinds=())

    with pytest.raises(ValueError, match="rclone container"):
        check_archive_prerequisites(
            destinations=[remote], rclone_container=container, rclone_config=config
        )
    container.touch()
    with pytest.raises(ValueError, match="need an rclone config"):
        check_archive_prerequisites(
            destinations=[remote], rclone_container=container, rclone_config=config
        )
    config.write_text("[dap]\ntype = s3\n")
    with pytest.raises(ValueError, match="defines no remote"):
        check_archive_prerequisites(
            destinations=[remote], rclone_container=container, rclone_config=config
        )
    config.write_text("[acacia]\ntype = s3\n")
    check_archive_prerequisites(
        destinations=[remote], rclone_container=container, rclone_config=config
    )
    with pytest.raises(ValueError, match="cannot write"):
        check_archive_prerequisites(
            destinations=[copy], rclone_container=container, rclone_config=None
        )
    check_archive_prerequisites(
        destinations=[_tar(tmp_path)], rclone_container=None, rclone_config=None
    )
