"""Tests around the casa self-calibration tooling"""

from __future__ import annotations

from pathlib import Path

from flint.casa import args_to_casa_task_string


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
