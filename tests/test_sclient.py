"""Some tests around the sclient utility"""

from __future__ import annotations

from pathlib import Path

import pytest

from flint.sclient import pull_container

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
