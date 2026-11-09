"""Tests around the archive flow"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from capn_crunch import create_options_from_parser
from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness

from flint import archive
from flint.options import ArchiveDestination, ArchiveFieldOptions, ArchiveOptions
from flint.prefect.flows.archive_pipeline import cli, get_parser, process_archive


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
            archive_field_options=ArchiveFieldOptions(
                field_path=field, rclone_container=tmp_path
            ),
            archive_options=options,
        )

    (call,) = calls
    assert call["command"][-2:] == [
        str(field.resolve()),
        str(tmp_path / "hot" / "SB60001"),
    ]
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
        pytest.raises(
            RuntimeError, match="SB60001 did not reach .*acacia: .*rclone container"
        ),
    ):
        # No rclone container, so the copy fails
        process_archive(
            archive_field_options=ArchiveFieldOptions(field_path=field),
            archive_options=options,
        )

    assert (tmp_path / "tape" / "SB60001.tar").exists()


def test_cli_refuses_a_missing_field_folder(tmp_path: Path) -> None:
    strategy = tmp_path / "s.yaml"
    strategy.write_text("version: 0.2\ndefaults: {}\n")

    with pytest.raises(FileNotFoundError, match="does not exist"):
        cli(
            [
                "--field-path",
                str(tmp_path / "60002"),
                "--imaging-strategy",
                str(strategy),
            ]
        )


def test_cli_needs_a_field_and_a_strategy() -> None:
    with pytest.raises(ValueError, match="both required"):
        cli(["--field-path", "60001"])


def test_parser() -> None:
    args = get_parser().parse_args(
        [
            "--field-path",
            "60001",
            "--imaging-strategy",
            "s.yaml",
            "--rclone-container",
            "r.sif",
        ]
    )

    options = create_options_from_parser(
        parser_namespace=args, options_class=ArchiveFieldOptions
    )

    assert options == ArchiveFieldOptions(
        field_path=Path("60001"),
        imaging_strategy=Path("s.yaml"),
        rclone_container=Path("r.sif"),
    )
    assert args.cluster_config == "petrichor"
