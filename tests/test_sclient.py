"""Some tests around the sclient utility"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.sclient import pull_container, run_singularity_command


@pytest.fixture(scope="session")
@pytest.mark.flaky(reruns=3, reruns_delay=2)
def hello_world_container(tmp_path_factory) -> Path:
    """Download the hello world container once and use it across the session"""
    temp_container_dir = Path(tmp_path_factory.mktemp("hello_world"))
    temp_container_dir.mkdir(parents=True, exist_ok=True)

    name = "example.sif"
    temp_container = temp_container_dir / name

    assert not temp_container.exists()

    output_path = pull_container(
        container_directory=temp_container_dir,
        uri="docker://alpine:latest",
        file_name=name,
    )
    return output_path


@pytest.mark.require_singularity
def test_pull_apptainer(hello_world_container):
    """Attempt to pull down an example container"""

    assert hello_world_container.exists()
    assert isinstance(hello_world_container, Path)


@pytest.mark.require_singularity
def test_run_singularity_command(hello_world_container):
    """Make sure that the running of a container works"""
    run_singularity_command(image=hello_world_container, command="echo 'JackSparrow'")


def test_raise_error_no_container_noexist() -> None:
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
