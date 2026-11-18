"""Tests around the subtract cube imaging flow"""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path

import pytest
from astropy.io import fits

from flint.coadd.linmos import LinmosResult
from flint.prefect.flows.subtract_cube_pipeline import (
    get_parser,
    task_combine_all_linmos_images,
)
from tests.test_cube import _write_channel_image


def test_get_parser():
    """Make sure the parser can actually be built"""
    # This is a silly one but I was bitten by it not working, arrrrhh matey
    _ = get_parser()


def _linmos_results(tmp_path: Path) -> list[LinmosResult]:
    """Three channel mosaics, as linmos leaves them"""
    return [
        LinmosResult(
            cmd="linmos",
            parset=tmp_path / f"ch{channel}_parset.txt",
            image_fits=_write_channel_image(
                tmp_path
                / f"SB1234.RACS_0000-00.ch{channel:04d}-{channel + 1:04d}.linmos.fits",
                channel=channel,
                shape=(5, 7),
            ),
            weight_fits=tmp_path / f"ch{channel}.weight.fits",
        )
        for channel in range(3)
    ]


def test_combine_all_linmos_images_replaces_a_partial_cube(tmp_path: Path):
    """A cube left half written by a dead worker is replaced"""
    linmos_results = _linmos_results(tmp_path)
    partial_cube = tmp_path / "SB1234.RACS_0000-00.contsub.freq.linmos.cube.fits"
    partial_cube.write_bytes(b"half written")

    cube = task_combine_all_linmos_images.fn(
        linmos_commands=linmos_results,
        update_fits_cube_options={"remove_original_images": False},
    )

    assert cube == partial_cube
    assert fits.getdata(cube).squeeze().shape == (3, 5, 7)


def test_combine_all_linmos_images_missing_image_keeps_the_cube(tmp_path: Path):
    """With an image missing the cube already on disk is left alone"""
    linmos_results = _linmos_results(tmp_path)
    existing_cube = tmp_path / "SB1234.RACS_0000-00.contsub.freq.linmos.cube.fits"
    existing_cube.write_bytes(b"an earlier cube")
    missing = linmos_results[1].image_fits
    missing.unlink()

    with pytest.raises(FileNotFoundError, match=re.escape(missing.name)):
        task_combine_all_linmos_images.fn(linmos_commands=linmos_results)

    assert existing_cube.read_bytes() == b"an earlier cube"


def test_combine_all_linmos_images_keeps_images_until_compressed(
    tmp_path: Path, monkeypatch
):
    """A worker killed while compressing leaves the images for the rerun"""
    linmos_results = _linmos_results(tmp_path)
    images = [linmos_result.image_fits for linmos_result in linmos_results]
    combine = partial(
        task_combine_all_linmos_images.fn,
        linmos_commands=linmos_results,
        update_fits_cube_options={"compress": True, "compress_method": "tile-gzip"},
    )

    def killed(*args, **kwargs):
        raise RuntimeError("killed")

    with monkeypatch.context() as patch:
        patch.setattr(
            "flint.prefect.flows.subtract_cube_pipeline.compress_cube", killed
        )
        with pytest.raises(RuntimeError, match="killed"):
            combine()
    assert all(image.exists() for image in images)

    cube = combine()

    assert cube.name.endswith(".fits.fz")
    assert not any(image.exists() for image in images)
