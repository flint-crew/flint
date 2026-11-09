"""Tests around the archive flow"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint import archive
from flint.options import ArchiveDestination, ArchiveOptions
from flint.prefect.flows.archive_pipeline import get_parser, process_archive


@pytest.fixture
def field(tmp_path: Path) -> Path:
    root = tmp_path / "60001"
    for name in ("SB60001.i.linmos.fits", "rmsynth/fdf.fits"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"contents of {name}")

    return root


def test_every_destination_gets_the_field(
    field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        archive, "run_singularity_command", lambda **kwargs: calls.append(kwargs)
    )
    options = ArchiveOptions(
        destinations={
            "hot": ArchiveDestination(
                method="copy",
                location=str(tmp_path / "hot"),
                patterns=(r".*\.linmos\.fits",),
            ),
            "tape": ArchiveDestination(
                method="tar", location=str(tmp_path / "tape"), patterns=(".*",)
            ),
        }
    )

    with prefect_test_harness(), disable_run_logger():
        process_archive(
            field_path=field, archive_options=options, rclone_container=tmp_path
        )

    (call,) = calls
    assert call["command"].endswith(
        f" {field.resolve()} {tmp_path / 'hot' / 'SB60001'}"
    )
    with tarfile.open(tmp_path / "tape" / "SB60001.tar") as tar:
        assert tar.getnames() == [
            "SB60001/SB60001.i.linmos.fits",
            "SB60001/rmsynth/fdf.fits",
        ]


def test_a_failed_destination_fails_the_flow_after_the_others_ran(
    field: Path, tmp_path: Path
) -> None:
    options = ArchiveOptions(
        destinations={
            "acacia": ArchiveDestination(
                method="copy", location="acacia:racs-all", patterns=(".*",)
            ),
            "tape": ArchiveDestination(
                method="tar", location=str(tmp_path / "tape"), patterns=(".*",)
            ),
        }
    )

    with (
        prefect_test_harness(),
        disable_run_logger(),
        pytest.raises(RuntimeError, match="SB60001 did not reach acacia"),
    ):
        # No rclone container, so the copy fails
        process_archive(field_path=field, archive_options=options)

    assert (tmp_path / "tape" / "SB60001.tar").exists()


def test_parser() -> None:
    args = get_parser().parse_args(
        ["60001", "--strategy-yaml-path", "s.yaml", "--rclone-container", "r.sif"]
    )

    assert args.field_path == Path("60001")
    assert args.rclone_container == Path("r.sif")
    assert args.rclone_config is None
    assert args.cluster_config == "petrichor"
