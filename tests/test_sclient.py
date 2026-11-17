"""Some tests around the sclient utility"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.sclient import pull_container, run_singularity_command


@pytest.fixture(scope="session")
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
@pytest.mark.flaky(reruns=3, reruns_delay=2)
def test_pull_apptainer(hello_world_container):
    """Attempt to pull down an example container"""

    assert hello_world_container.exists()
    assert isinstance(hello_world_container, Path)


@pytest.mark.require_singularity
@pytest.mark.flaky(reruns=3, reruns_delay=2)
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


def test_read_only_binds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only bind takes the :ro form, and wins over a read-write bind"""
    from flint import sclient

    image = tmp_path / "image.sif"
    image.touch()
    shared, writable = tmp_path / "shared", tmp_path / "writable"
    calls: list[dict] = []

    def fake_execute(**kwargs) -> list[str]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(sclient.sclient, "execute", fake_execute)
    monkeypatch.setattr(sclient, "sleep", lambda seconds: None)

    run_singularity_command(
        image=image,
        command="ls",
        bind_dirs=[shared, writable],
        read_only_bind_dirs=[shared],
    )

    assert sorted(calls[0]["bind"]) == [
        f"{shared}:{shared}:ro",
        str(writable),
    ]


@pytest.mark.require_singularity
def test_read_only_bind_cannot_be_written(hello_world_container, tmp_path: Path):
    from subprocess import CalledProcessError

    with pytest.raises(CalledProcessError):
        run_singularity_command(
            image=hello_world_container,
            command=f"touch {tmp_path}/written",
            read_only_bind_dirs=[tmp_path],
        )
