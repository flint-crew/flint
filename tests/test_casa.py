"""Tests around the casa self-calibration tooling"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from flint.casa import args_to_casa_task_string, copy_with_mstranform


def test_args_to_casa_task_str():
    """Ensure we can transform casa style function calls to strings"""

    transform = args_to_casa_task_string(
        task="mstransform",
        regridms=True,
        nspw=1,
        mode="channel",
        nchan=-1,
    )
    assert isinstance(transform, str)

    expected = "casa -c mstransform(regridms=True,nspw=1,mode='channel',nchan=-1)"
    assert transform == expected


def test_args_to_casa_task_str_arg_list():
    """Same as above but should an argument be a list of paths (for example) needs help"""

    paths = [
        Path("/jack/dataset1.ms"),
        Path("/jack/dataset2.ms"),
    ]
    applycal = args_to_casa_task_string(
        task="applycal",
        vis=Path("/some/other/ship/visibility.ms"),
        gaintable=paths,
    )

    expected = "casa -c applycal(vis='/some/other/ship/visibility.ms',gaintable=('/jack/dataset1.ms','/jack/dataset2.ms'))"
    assert expected == applycal


def test_copy_with_mstranform(tmp_path, monkeypatch):
    """Writes to a new path when given one, otherwise replaces the input"""

    def fake_run(image, command, bind_dirs, *args, **kwargs):
        Path(re.search(r"outputvis='([^']+)'", command).group(1)).mkdir()

    monkeypatch.setattr("flint.sclient.run_singularity_command", fake_run)

    container = tmp_path / "casa.sif"
    ms_path = tmp_path / "input.ms"
    ms_path.mkdir()
    (ms_path / "original").touch()

    output_ms = tmp_path / "output" / "output.ms"
    result = copy_with_mstranform(
        casa_container=container, ms_path=ms_path, output_ms_path=output_ms
    )
    assert result == output_ms
    assert output_ms.exists()
    assert (ms_path / "original").exists()

    with pytest.raises(FileExistsError):
        copy_with_mstranform(
            casa_container=container, ms_path=ms_path, output_ms_path=output_ms
        )

    result = copy_with_mstranform(casa_container=container, ms_path=ms_path)
    assert result == ms_path
    assert not (ms_path / "original").exists()
    assert not list(tmp_path.glob("*.ms_transform"))
