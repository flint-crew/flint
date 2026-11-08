"""Tests around the archive flow"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from prefect import task
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint import archive
from flint.manifest import (
    MANIFEST_NAME,
    ArchiveDestination,
    ArchiveManifest,
    ArchiveProduct,
    read_manifest,
    write_manifest,
)
from flint.prefect.flows import archive_pipeline


def _manifest_file(
    tmp_path: Path, destinations: tuple[ArchiveDestination, ...]
) -> Path:
    """An unsealed manifest of a field with two products"""
    root = tmp_path / "60001"
    root.mkdir()
    for name in ("i.linmos.fits", "q.cube.fits"):
        (root / name).write_text(f"contents of {name}")
    manifest = ArchiveManifest(
        field="SB60001",
        sbids={"low": 60001},
        root=root,
        products=(
            ArchiveProduct(path=Path("i.linmos.fits"), kind="image"),
            ArchiveProduct(path=Path("q.cube.fits"), kind="cube"),
        ),
        destinations=destinations,
    )
    return write_manifest(manifest=manifest, path=root / MANIFEST_NAME)


def _tar(tmp_path: Path) -> ArchiveDestination:
    (tmp_path / "tape").mkdir(exist_ok=True)
    return ArchiveDestination(
        name="tar", location=str(tmp_path / "tape"), kinds=("image", "cube")
    )


def _remote(name: str) -> ArchiveDestination:
    return ArchiveDestination(name=name, location=f"{name}:bucket", kinds=("image",))


def test_a_failed_remote_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remotes run one after another, and the next still runs after one fails"""
    started: list[str] = []
    lock = threading.Lock()

    @task
    def fake_archive_to(manifest, destination, rclone_container, rclone_config):
        with lock:
            started.append(destination.name)
        if destination.name == "first":
            raise RuntimeError("no write access")
        return destination

    monkeypatch.setattr(archive_pipeline, "task_archive_to", fake_archive_to)
    destinations = (
        _remote("first"),
        _tar(tmp_path),
        _remote("second"),
        ArchiveDestination(name="copy", location=str(tmp_path / "hot"), kinds=()),
    )
    manifest_path = _manifest_file(tmp_path, destinations)

    with prefect_test_harness(), disable_run_logger():
        with pytest.raises(RuntimeError, match="archive stage did not complete"):
            archive_pipeline.process_archive(manifest_path=manifest_path)

    assert sorted(started) == ["copy", "first", "second", "tar"]
    assert started.index("first") < started.index("second")
    assert read_manifest(manifest_path).sealed is not None


def test_a_missing_product_stops_the_archive_before_any_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_file(tmp_path, (_tar(tmp_path),))
    (manifest_path.parent / "q.cube.fits").unlink()
    archive_to = MagicMock()
    monkeypatch.setattr(archive_pipeline, "task_archive_to", archive_to)

    with prefect_test_harness(), disable_run_logger():
        with pytest.raises(RuntimeError, match="archive sealing"):
            archive_pipeline.process_archive(manifest_path=manifest_path)

    archive_to.submit.assert_not_called()
    assert read_manifest(manifest_path).sealed is None


def test_a_rerun_skips_destinations_that_are_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_file(tmp_path, (_tar(tmp_path),))

    with prefect_test_harness(), disable_run_logger():
        first = archive_pipeline.process_archive(manifest_path=manifest_path)
        monkeypatch.setattr(
            archive, "write_tarball", lambda **kwargs: pytest.fail("rewritten")
        )
        second = archive_pipeline.process_archive(manifest_path=manifest_path)

    assert second.sealed == first.sealed
    assert len(list((tmp_path / "tape").glob("*.tar"))) == 1


def test_destination_tasks_carry_the_concurrency_tag() -> None:
    assert "flint-archive" in archive_pipeline.task_archive_to.tags


def test_check_exits_with_one_until_the_field_is_archived(tmp_path: Path) -> None:
    manifest_path = _manifest_file(tmp_path, (_tar(tmp_path),))

    with pytest.raises(SystemExit) as exit_info:
        archive_pipeline.cli(["check", str(manifest_path)])
    assert exit_info.value.code == 1

    with prefect_test_harness(), disable_run_logger():
        archive_pipeline.process_archive(manifest_path=manifest_path)

    with pytest.raises(SystemExit) as exit_info:
        archive_pipeline.cli(["check", str(manifest_path)])
    assert exit_info.value.code == 0


def test_run_adds_destinations_to_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest_file(tmp_path, ())
    (tmp_path / "tape").mkdir()
    flow = MagicMock()
    monkeypatch.setattr(archive_pipeline, "process_archive", flow)
    monkeypatch.setattr(archive_pipeline, "get_dask_runner", lambda cluster: None)

    archive_pipeline.cli(
        ["run", str(manifest_path), "--tar-path", str(tmp_path / "tape")]
    )

    (destination,) = read_manifest(manifest_path).destinations
    assert destination.name == "tar"
    assert flow.with_options.return_value.call_args.kwargs == {
        "manifest_path": manifest_path,
        "rclone_container": None,
        "rclone_config": None,
    }


@pytest.mark.slow
@pytest.mark.require_singularity
def test_process_archive_with_real_rclone(rclone_image: Path, tmp_path: Path) -> None:
    """Hot copy and tarball, end to end, then a check that both are complete"""
    destinations = (
        ArchiveDestination(
            name="copy", location=str(tmp_path / "hot"), kinds=("image",)
        ),
        _tar(tmp_path),
    )
    manifest_path = _manifest_file(tmp_path, destinations)

    with prefect_test_harness(), disable_run_logger():
        archive_pipeline.process_archive(
            manifest_path=manifest_path, rclone_container=rclone_image
        )

    assert (tmp_path / "hot" / "SB60001" / "i.linmos.fits").exists()
    assert not (tmp_path / "hot" / "SB60001" / "q.cube.fits").exists()
    with pytest.raises(SystemExit) as exit_info:
        archive_pipeline.cli(
            ["check", str(manifest_path), "--rclone-container", str(rclone_image)]
        )
    assert exit_info.value.code == 0
