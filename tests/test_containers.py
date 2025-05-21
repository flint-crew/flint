"""Items to test functionality around the containers interface"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.containers import (
    KNOWN_CONTAINER_LOOKUP,
    LIST_OF_KNOWN_CONTAINERS,
    FlintContainer,
    _sanity_check_containers,
    get_known_container_path,
    log_known_containers,
    verify_known_containers,
)


def test_sanity_check_containers():
    """Make sure that the reference containers are valid with a simple set of checks.
    Make sure not doubling up on the reference name of file name"""
    _sanity_check_containers(container_list=LIST_OF_KNOWN_CONTAINERS)
    from copy import deepcopy

    dummy = list(deepcopy(LIST_OF_KNOWN_CONTAINERS))
    dummy.append(dummy[0])

    with pytest.raises(AssertionError):
        _sanity_check_containers(container_list=dummy)


def test_verify_known_containers(tmpdir):
    """Check that our verify function works. This will cheat and
    create some temporary file with the expected file name as the
     verify function in of itself (currently) only checks to see
     if a file exists"""
    container_directory = Path(tmpdir) / "containers1"
    container_directory.mkdir(parents=True)

    assert not verify_known_containers(container_directory=container_directory)

    for cata in LIST_OF_KNOWN_CONTAINERS:
        cata_path = container_directory / cata.file_name
        cata_path.touch()

    assert verify_known_containers(container_directory=container_directory)


def test_all_flint_containers():
    """Make sure everything we know is a FlintContainer"""
    assert all([isinstance(fc, FlintContainer) for fc in LIST_OF_KNOWN_CONTAINERS])


def test_all_known_containers():
    """Same as above but for the known donctainers lookup"""
    for k, v in KNOWN_CONTAINER_LOOKUP.items():
        assert isinstance(k, str)
        assert isinstance(v, FlintContainer)


def test_log_containers():
    """Output all the known containers"""
    # This should simply not error
    log_known_containers()


@pytest.mark.slow
@pytest.mark.require_singularity
def test_download_flint_containers(flint_containers) -> None:
    """Start the download of the flint containers"""
    assert isinstance(flint_containers, Path)
    assert flint_containers.exists()


@pytest.mark.slow
@pytest.mark.require_singularity
def test_verify_containers_with_containers(flint_containers):
    """Make sure the actual containers downloaded register as correct. This
    uses the download fixture"""
    assert verify_known_containers(container_directory=flint_containers)


@pytest.mark.slow
@pytest.mark.require_singularity
def test_get_known_container_path(flint_containers):
    """Get the to a known container"""
    casa_container = get_known_container_path(
        container_directory=flint_containers, name="casa"
    )
    assert isinstance(casa_container, Path)
    assert casa_container.exists()

    with pytest.raises(ValueError):
        casa_container = get_known_container_path(
            container_directory=flint_containers, name="JackSparrowNotBeKnown"
        )
