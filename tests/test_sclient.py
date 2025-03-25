"""Some tests around the sclient utility"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.sclient import pull_container, run_singularity_command

from .test_helpers import which

if which("singularity") is None:
    pytest.skip("Singularity is not installed", allow_module_level=True)


def test_pull_apptainer(tmpdir):
    """Attempt to pull down an example container"""
    temp_container_dir = Path(tmpdir) / "container_dl"
    temp_container_dir.mkdir(parents=True, exist_ok=True)

    name = "example.sif"
    temp_container = temp_container_dir / name

    assert not temp_container.exists()

    output_path = pull_container(
        container_directory=temp_container_dir,
        uri="docker://hello-world:linux",
        file_name=name,
    )

    assert temp_container.exists()
    assert isinstance(output_path, Path)
    assert output_path == temp_container


def test_raise_error_no_container() -> None:
    """Should an incorrect path be given an error should be raised"""

    no_exists_container = Path("JackBeNotHereMate.sif")
    assert not no_exists_container.exists()

    with pytest.raises(FileNotFoundError):
        run_singularity_command(image=no_exists_container, command="PiratesbeHere")


def test_positive_max_retries() -> None:
    """Make sure an error is raised if `max_retries` reaches break case"""
    no_exists_container = Path("JackBeNotHereMate.sif")
    assert not no_exists_container.exists()

    # These errors are fired off before the check for the container is made
    with pytest.raises(ValueError):
        run_singularity_command(
            image=no_exists_container, command="PiratesbeHere", max_retries=0
        )
    with pytest.raises(ValueError):
        run_singularity_command(
            image=no_exists_container, command="PiratesbeHere", max_retries=-222
        )

    with pytest.raises(FileNotFoundError):
        run_singularity_command(
            image=no_exists_container, command="PiratesbeHere", max_retries=111
        )
