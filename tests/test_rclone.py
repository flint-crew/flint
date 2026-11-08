"""Tests around running rclone from its container"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError

import pytest

from flint import rclone
from flint.manifest import ArchiveManifest, ArchiveProduct


@pytest.fixture
def rclone_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record each rclone command, and the files it was handed, rather than run it"""
    calls: list[dict] = []

    def fake_run(image: Path, command: str, bind_dirs, read_only_bind_dirs) -> None:
        handed = {
            Path(word): Path(word).read_text()
            for word in command.split()
            if word.startswith("/") and Path(word).is_file()
        }
        calls.append(
            {
                "command": command,
                "bind_dirs": list(bind_dirs),
                "read_only_bind_dirs": list(read_only_bind_dirs),
                "files": handed,
            }
        )

    monkeypatch.setattr(rclone, "run_singularity_command", fake_run)
    return calls


def test_config_remotes(tmp_path: Path) -> None:
    config = tmp_path / "rclone.conf"
    config.write_text("[acacia]\ntype = s3\n\n[dap]\ntype = s3\n")

    assert rclone.config_remotes(config) == {"acacia", "dap"}


def test_copy_then_check_against_the_products_md5s(
    tmp_path: Path, rclone_calls: list[dict]
) -> None:
    root, config = tmp_path / "root", tmp_path / "rclone.conf"
    products = [
        ArchiveProduct(path=Path("i.fits"), kind="image", size=1, md5="aa"),
        ArchiveProduct(path=Path("rmsynth/fdf.fits"), kind="rm", size=2, md5="bb"),
    ]

    rclone.rclone_copy(
        container=tmp_path / "rclone.sif",
        config=config,
        root=root,
        products=products,
        location="acacia:racs-all/SB60001",
    )

    copy, check = rclone_calls
    assert copy["command"].startswith(f"rclone --config {config} copy --files-from ")
    assert copy["command"].endswith(
        f"--checksum --immutable {root} acacia:racs-all/SB60001"
    )
    assert list(copy["files"].values()) == ["i.fits\nrmsynth/fdf.fits\n"]
    assert copy["read_only_bind_dirs"] == [root, config]
    assert check["command"].startswith(
        f"rclone --config {config} checksum md5 --one-way "
    )
    assert list(check["files"].values()) == ["aa  i.fits\nbb  rmsynth/fdf.fits\n"]
    assert root not in check["read_only_bind_dirs"]


def test_a_local_location_is_made_and_bound(
    tmp_path: Path, rclone_calls: list[dict]
) -> None:
    location = tmp_path / "hot" / "SB60001"

    rclone.rclone_copy(
        container=tmp_path / "rclone.sif",
        config=None,
        root=tmp_path,
        products=[],
        location=str(location),
    )

    assert location.is_dir()
    assert all(location in call["bind_dirs"] for call in rclone_calls)
    assert all("--config" not in call["command"] for call in rclone_calls)


def test_an_unreadable_marker_is_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**kwargs) -> None:
        raise CalledProcessError(returncode=3, cmd="rclone copyto")

    monkeypatch.setattr(rclone, "run_singularity_command", fail)

    assert (
        rclone.rclone_read_marker(
            container=tmp_path / "rclone.sif", config=None, location="acacia:a"
        )
        is None
    )


def test_a_marker_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake copies the marker to and from a folder standing in for the remote"""
    remote = tmp_path / "remote"
    remote.mkdir()

    def fake_copyto(image: Path, command: str, bind_dirs, read_only_bind_dirs) -> None:
        source, target = command.split()[-2:]
        source = source.replace("acacia:", f"{remote}/")
        target = target.replace("acacia:", f"{remote}/")
        Path(target).write_text(Path(source).read_text())

    monkeypatch.setattr(rclone, "run_singularity_command", fake_copyto)
    manifest = ArchiveManifest(
        field="SB60001",
        sbids={},
        root=tmp_path,
        sealed=datetime(2026, 11, 7, tzinfo=UTC),
    )
    container = tmp_path / "rclone.sif"

    rclone.rclone_write_marker(
        container=container, config=None, manifest=manifest, location="acacia:"
    )

    assert (
        rclone.rclone_read_marker(container=container, config=None, location="acacia:")
        == manifest
    )


@pytest.fixture(scope="session")
def rclone_image(tmp_path_factory) -> Path:
    """The real rclone container, pulled once for the session"""
    from flint.containers import rclone_container
    from flint.sclient import pull_container

    return pull_container(
        container_directory=tmp_path_factory.mktemp("rclone"),
        uri=f"{rclone_container.uri}:{rclone_container.tag}",
        file_name=rclone_container.file_name,
    )


@pytest.mark.slow
@pytest.mark.require_singularity
def test_rclone_to_a_local_remote(rclone_image: Path, tmp_path: Path) -> None:
    """Copy, check and mark against a ``type = local`` remote, standing in for S3"""
    from flint.manifest import md5_file

    root = tmp_path / "root"
    (root / "rmsynth").mkdir(parents=True)
    for name in ("i.fits", "rmsynth/fdf.fits"):
        (root / name).write_text(f"contents of {name}")
    products = [
        ArchiveProduct(
            path=Path(name),
            kind="image",
            size=(root / name).stat().st_size,
            md5=md5_file(root / name),
        )
        for name in ("i.fits", "rmsynth/fdf.fits")
    ]
    config = tmp_path / "rclone.conf"
    config.write_text("[local1]\ntype = local\n")
    destination = tmp_path / "remote"
    location = f"local1:{destination}/SB60001"
    manifest = ArchiveManifest(
        field="SB60001",
        sbids={},
        root=root,
        sealed=datetime(2026, 11, 7, tzinfo=UTC),
        products=tuple(products),
    )
    arguments = dict(container=rclone_image, config=config)

    rclone.rclone_copy(**arguments, root=root, products=products, location=location)
    rclone.rclone_write_marker(**arguments, manifest=manifest, location=location)

    assert (destination / "SB60001" / "rmsynth" / "fdf.fits").exists()
    assert rclone.rclone_read_marker(**arguments, location=location) == manifest
    assert rclone.rclone_read_marker(**arguments, location=f"{location}-none") is None

    (destination / "SB60001" / "i.fits").write_text("tampered")
    with pytest.raises(CalledProcessError):
        rclone.rclone_copy(**arguments, root=root, products=products, location=location)
